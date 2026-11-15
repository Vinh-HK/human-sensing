"""Tableau de bord web du hub : plan en direct, pièces, nœuds, alertes.

Serveur HTTP de la bibliothèque standard (aucune dépendance). Il tourne dans un
fil à part et lit l'état du moteur sous verrou.

  GET /            la page
  GET /api/plan    pièces, portes, nœuds (une fois au chargement)
  GET /api/state   pistes, occupation, lumières, nœuds, alertes (toutes les secondes)

Pas d'authentification : à n'exposer que sur le réseau local.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def plan_of(apt) -> dict:
    return {
        "rooms": [{"name": r.name, "polygon": r.polygon} for r in apt.rooms.values()],
        "doors": [{"name": d.name, "a": d.a, "b": d.b} for d in apt.doors],
        "nodes": [{"id": n.id, "room": n.room, "x": n.pose[0], "y": n.pose[1],
                   "theta_deg": math.degrees(n.pose[2])} for n in apt.nodes.values()],
    }


def state_of(engine, now: float, truth=None) -> dict:
    snap = engine.snapshot()
    al = engine.alerts.snapshot()
    nodes = {}
    for nid in engine.apt.nodes:
        last = engine.last_frame[nid]
        level = "ok"
        for a in al["active"]:
            if a["node"] == nid:
                level = "erreur" if a["level"] == "erreur" or level == "erreur" else "attention"
        nodes[nid] = {"age_s": None if last is None else round(now - last, 1), "level": level}
    out = {"t": round(now, 1), **snap, "nodes": nodes, "alerts": al}
    if truth is not None:
        out["truth"] = truth
    return out


class Dashboard:
    def __init__(self, apt, engine, lock: threading.Lock, clock, port: int = 8080,
                 host: str = "0.0.0.0", truth=None):
        self.apt, self.engine, self.lock, self.clock = apt, engine, lock, clock
        self.port, self.host, self.truth = port, host, truth
        self.httpd = None

    def start(self) -> None:
        dash = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # pas de journal à chaque requête
                pass

            def _send(self, code, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/plan":
                    self._send(200, json.dumps(plan_of(dash.apt)).encode(), "application/json")
                elif path == "/api/state":
                    with dash.lock:
                        truth = dash.truth() if dash.truth else None
                        body = json.dumps(state_of(dash.engine, dash.clock(), truth), ensure_ascii=False)
                    self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                else:
                    self._send(404, b"introuvable", "text/plain")

        self.httpd = ThreadingHTTPServer((self.host, self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        log.warning("tableau de bord : http://%s:%d", "localhost" if self.host == "0.0.0.0" else self.host, self.port)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()


PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Présence — tableau de bord</title>
<style>
  :root { --bg:#f6f5f1; --fg:#1d1d1b; --muted:#77746c; --line:#d6d1c6; --card:#fff; --room:#fff;
    --accent:#2a6fdb; --held:#8a5cd6; --door:#e08a2c; --light:#f5c542; --warn:#e08a2c; --err:#d64545; --ok:#2e9d5b; }
  @media (prefers-color-scheme: dark) { :root { --bg:#141413; --fg:#ecebe6; --muted:#9b978d; --line:#3d3b37;
    --card:#1e1e1c; --room:#1e1e1c; --accent:#6aa2ff; --held:#b08cff; --door:#e8a050; --light:#e8b830;
    --warn:#e8a050; --err:#ff6b6b; --ok:#4fc483; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { display:flex; align-items:center; gap:12px; padding:14px 20px; border-bottom:1px solid var(--line); }
  header h1 { font-size:17px; margin:0; }
  #status { margin-left:auto; font-size:13px; color:var(--muted); }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:0; }
  main { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(300px,1fr); gap:16px; padding:16px 20px; max-width:1200px; margin:0 auto; }
  @media (max-width:820px) { main { grid-template-columns:1fr; } }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:14px; }
  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 8px; }
  svg { width:100%; height:auto; display:block; }
  .alert { border-left:4px solid var(--c); padding:6px 10px; margin:6px 0; background:color-mix(in srgb, var(--c) 9%, transparent); border-radius:4px; }
  .alert b { display:block; }
  .alert small { color:var(--muted); }
  .none { color:var(--muted); font-size:13px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td, th { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  .bulb { display:inline-block; width:12px; height:12px; border-radius:50%; border:1px solid var(--line); vertical-align:-2px; }
  .hist li { font-size:12.5px; color:var(--muted); }
  .legend { display:flex; gap:12px; flex-wrap:wrap; font-size:12px; color:var(--muted); margin-top:8px; }
</style>
</head>
<body>
<header><h1>Présence — tableau de bord</h1><span id="status">connexion…</span></header>
<main>
  <div>
    <div class="card">
      <svg id="plan" role="img" aria-label="Plan de l'appartement avec les personnes suivies"></svg>
      <div class="legend">
        <span><span class="dot" style="background:var(--accent)"></span>en mouvement</span>
        <span><span class="dot" style="background:var(--held)"></span>immobile</span>
        <span><span class="dot" style="background:var(--light)"></span>lumière allumée</span>
        <span><span class="dot" style="background:var(--err)"></span>nœud en alerte</span>
        <span id="truth-legend" hidden><span class="dot" style="background:var(--muted)"></span>position réelle (démo)</span>
      </div>
    </div>
  </div>
  <div>
    <div class="card"><h2>Alertes actives</h2><div id="alerts"></div></div>
    <div class="card"><h2>Pièces</h2><table id="rooms"></table></div>
    <div class="card"><h2>Nœuds radar</h2><table id="nodes"></table></div>
    <div class="card"><h2>Historique des alertes</h2><ul class="hist" id="hist" style="margin:0;padding-left:18px"></ul></div>
  </div>
</main>
<script>
const NS = "http://www.w3.org/2000/svg";
let plan = null, S = 60, X, Y;
const mk = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const levelColor = { info: "var(--accent)", attention: "var(--warn)", erreur: "var(--err)" };
const glows = {}, nodeEls = {}; let gTracks, gTruth;

function drawPlan(p) {
  const pts = p.rooms.flatMap(r => r.polygon);
  const minx = Math.min(...pts.map(q => q[0])), maxx = Math.max(...pts.map(q => q[0]));
  const miny = Math.min(...pts.map(q => q[1])), maxy = Math.max(...pts.map(q => q[1]));
  const m = 30; X = x => m + (x - minx) * S; Y = y => m + (maxy - y) * S;
  const svg = document.getElementById("plan");
  svg.setAttribute("viewBox", `0 0 ${(maxx - minx) * S + 2 * m} ${(maxy - miny) * S + 2 * m}`);
  for (const r of p.rooms) {
    const d = r.polygon.map(q => `${X(q[0])},${Y(q[1])}`).join(" ");
    mk("polygon", { points: d, fill: "var(--room)", stroke: "var(--fg)", "stroke-width": 2 }, svg);
    glows[r.name] = mk("polygon", { points: d, fill: "var(--light)", opacity: 0 }, svg);
    glows[r.name].style.transition = "opacity .4s";
    const cx = r.polygon.reduce((a, q) => a + q[0], 0) / r.polygon.length;
    const cy = r.polygon.reduce((a, q) => a + q[1], 0) / r.polygon.length;
    const t = mk("text", { x: X(cx), y: Y(cy), "text-anchor": "middle", fill: "var(--muted)", "font-size": 13 }, svg);
    t.textContent = r.name;
  }
  for (const d of p.doors)
    mk("line", { x1: X(d.a[0]), y1: Y(d.a[1]), x2: X(d.b[0]), y2: Y(d.b[1]), stroke: "var(--door)", "stroke-width": 7 }, svg);
  for (const n of p.nodes) {
    const g = mk("g", {}, svg);
    const th = n.theta_deg * Math.PI / 180;
    mk("line", { x1: X(n.x), y1: Y(n.y), x2: X(n.x + 0.45 * Math.cos(th)), y2: Y(n.y + 0.45 * Math.sin(th)),
      stroke: "var(--accent)", "stroke-width": 2 }, g);
    const sq = mk("rect", { x: X(n.x) - 6, y: Y(n.y) - 6, width: 12, height: 12, rx: 2, fill: "var(--accent)" }, g);
    const tt = mk("title", {}, sq); tt.textContent = n.id;
    nodeEls[n.id] = sq;
  }
  gTruth = mk("g", {}, svg); gTracks = mk("g", {}, svg);
}

function render(s) {
  document.getElementById("status").innerHTML =
    `<span class="dot" style="background:var(--ok)"></span>en direct · t = ${s.t.toFixed(0)} s`;
  for (const [r, on] of Object.entries(s.lights)) if (glows[r]) glows[r].setAttribute("opacity", on ? 0.45 : 0);
  gTracks.replaceChildren();
  for (const tr of s.tracks) {
    const held = tr.state === "immobile";
    if (held) mk("circle", { cx: X(tr.x), cy: Y(tr.y), r: 15, fill: "none", stroke: "var(--held)",
      "stroke-width": 2, "stroke-dasharray": "4 4" }, gTracks);
    mk("circle", { cx: X(tr.x), cy: Y(tr.y), r: 8, fill: held ? "var(--held)" : "var(--accent)",
      stroke: "var(--bg)", "stroke-width": 2 }, gTracks);
    const t = mk("text", { x: X(tr.x), y: Y(tr.y) - 13, "text-anchor": "middle", "font-size": 11,
      "font-weight": 700, fill: "var(--fg)" }, gTracks);
    t.textContent = "#" + tr.id;
  }
  gTruth.replaceChildren();
  if (s.truth) {
    document.getElementById("truth-legend").hidden = false;
    for (const p of s.truth) mk("circle", { cx: X(p[0]), cy: Y(p[1]), r: 4, fill: "var(--muted)", opacity: .7 }, gTruth);
  }
  for (const [id, n] of Object.entries(s.nodes)) if (nodeEls[id])
    nodeEls[id].setAttribute("fill", n.level === "erreur" ? "var(--err)" : n.level === "attention" ? "var(--warn)" : "var(--accent)");

  const act = s.alerts.active;
  document.getElementById("alerts").innerHTML = act.length ? act.map(a =>
    `<div class="alert" style="--c:${levelColor[a.level]}"><b>${esc(a.title)}</b>${esc(a.detail)}
     <br><small>${a.level} · depuis ${Math.round(s.t - a.since)} s</small></div>`).join("")
    : `<div class="none">Aucune alerte. Tout fonctionne.</div>`;

  document.getElementById("rooms").innerHTML = "<tr><th>Pièce</th><th>Personnes</th><th>Lumière</th></tr>" +
    Object.keys(s.occupancy).map(r => `<tr><td>${esc(r)}</td><td>${s.occupancy[r]}</td>
      <td><span class="bulb" style="background:${s.lights[r] ? "var(--light)" : "transparent"}"></span></td></tr>`).join("");

  document.getElementById("nodes").innerHTML = "<tr><th>Nœud</th><th>Dernière trame</th><th>État</th></tr>" +
    Object.entries(s.nodes).map(([id, n]) => `<tr><td>${esc(id)}</td>
      <td>${n.age_s === null ? "jamais" : n.age_s < 1 ? "à l'instant" : "il y a " + n.age_s.toFixed(0) + " s"}</td>
      <td><span class="dot" style="background:${n.level === "erreur" ? "var(--err)" : n.level === "attention" ? "var(--warn)" : "var(--ok)"}"></span>${n.level}</td></tr>`).join("");

  const h = s.alerts.history;
  document.getElementById("hist").innerHTML = h.length ? h.map(a =>
    `<li>${esc(a.title)} — ${a.active ? "en cours" : "résolue après " + Math.round(a.ended - a.since) + " s"}</li>`).join("")
    : `<li>vide</li>`;
}

async function loop() {
  try {
    if (!plan) { plan = await (await fetch("api/plan")).json(); drawPlan(plan); }
    render(await (await fetch("api/state")).json());
  } catch (e) {
    document.getElementById("status").innerHTML = `<span class="dot" style="background:var(--err)"></span>hub injoignable`;
  }
  setTimeout(loop, 1000);
}
loop();
</script>
</body>
</html>
"""
