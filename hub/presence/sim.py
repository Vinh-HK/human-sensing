"""Simulateur d'appartement : personnes scriptées -> trames LD2450/LD2410C -> moteur.

Il sert à valider la logique sans matériel, et à mesurer l'effet des
paramètres. Le modèle de capteur est volontairement pessimiste ; ses
hypothèses (ci-dessous) sont À VÉRIFIER sur le matériel réel en TP :

  LD2450 (suivi) :
    - p(détection) = 0.95 en mouvement ;
    - immobile : 0.7 pendant 4 s, puis 0.08 (le LD2450 « perd » les
      personnes figées, c'est le comportement connu des radars de suivi) ;
    - bruit gaussien sigma = 0.12 m ; 2 personnes à moins de 0.5 m -> 1 cible ;
    - 3 cibles max par capteur ; fantômes isolés p = 0.02 par trame.
  LD2410C (présence statique, respiration) :
    - p(présent | quelqu'un dans la pièce) = 0.98 ; faux positif 0.003 par trame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .config import Apartment
from .engine import Engine
from .geometry import dist_point_polygon, in_fov, world_to_sensor

DT = 0.1


@dataclass
class Person:
    name: str
    script: list
    t0: float = 0.0
    start: tuple[float, float] = (9.0, 4.6)   # hors appartement par défaut
    speed: float = 1.1
    # état interne
    pos: np.ndarray = field(default=None)
    vel: float = 0.0
    still_since: float | None = None
    fidget: bool = False
    _i: int = 0
    _wp: list = field(default_factory=list)
    _until: float = -1.0
    _anchor: np.ndarray = None

    def __post_init__(self):
        self.pos = np.array(self.start, dtype=float)

    def step(self, t: float, dt: float) -> None:
        if t < self.t0:
            self._mark_still(t, fidget=False)
            return
        if self._wp:
            target = np.array(self._wp[0], dtype=float)
            d = target - self.pos
            dist = float(np.hypot(*d))
            stepl = self.speed * dt
            if dist <= stepl:
                self.pos = target
                self._wp.pop(0)
            else:
                self.pos = self.pos + d / dist * stepl
            self.vel = self.speed
            self.still_since = None
            return
        if t < self._until:
            if self.fidget:  # petits mouvements autour d'un point fixe
                self.pos = self._anchor + np.random.normal(0, 0.05, 2)
            return
        if self._i >= len(self.script):
            self._mark_still(t, self.fidget)
            return
        cmd = self.script[self._i]
        self._i += 1
        if cmd[0] == "go":
            self._wp = [list(p) for p in cmd[1]]
        elif cmd[0] == "stay":
            self._until = t + cmd[1]
            self._anchor = self.pos.copy()
            self._mark_still(t, fidget=(len(cmd) > 2 and cmd[2] == "fidget"))
        elif cmd[0] == "warp":
            self.pos = np.array(cmd[1], dtype=float)

    def _mark_still(self, t, fidget):
        self.vel = 0.0
        self.fidget = fidget
        if self.still_since is None:
            self.still_since = t


# Scénario par défaut, sur l'appartement de config.example.yaml.
# Alice rentre, s'assoit au salon, va en cuisine, revient, va se coucher.
# Bob dort déjà au démarrage du système, se lève, va à la sdb, puis sort.
DEFAULT_SCENARIO = dict(
    duration=1500.0,
    persons=[
        dict(name="Alice", t0=5, start=(8.6, 4.6), script=[
            ("go", [(7.5, 4.6), (2.5, 4.6), (2.5, 3.4), (1.5, 1.5)]),
            ("stay", 300, "fidget"),                       # canapé, téléphone
            ("go", [(3.0, 1.5), (5.0, 1.5), (6.5, 2.0)]),
            ("stay", 90, "fidget"),                        # cuisine
            ("go", [(5.0, 1.5), (2.0, 2.5)]),
            ("stay", 240),                                 # assise, immobile
            ("go", [(2.5, 3.4), (2.5, 4.6), (1.5, 4.6), (1.5, 6.0), (1.0, 8.0)]),
            ("stay", 600),                                 # couchée
        ]),
        dict(name="Bob", t0=0, start=(3.0, 8.0), script=[
            ("stay", 200),                                 # dort au boot du système
            ("go", [(1.5, 6.0), (1.5, 4.6), (4.9, 4.6), (4.9, 6.0), (5.5, 7.0)]),
            ("stay", 180, "fidget"),                       # douche
            ("go", [(4.9, 6.0), (4.9, 4.6), (7.6, 4.6), (8.6, 4.6), (10, 4.6)]),
        ]),
        dict(name="Chloé", t0=700, start=(8.6, 4.6), script=[
            ("go", [(7.5, 4.6), (2.5, 4.6), (2.5, 3.4), (1.2, 2.0)]),
            ("stay", 200, "fidget"),                       # rejoint Alice… qui part se coucher
            ("go", [(2.5, 3.4), (2.5, 4.6), (7.6, 4.6), (8.6, 4.6), (10, 4.6)]),
        ]),
    ],
)


@dataclass
class SensorModel:
    p_move: float = 0.95
    p_still_early: float = 0.7
    p_still_late: float = 0.08
    still_fade_s: float = 4.0
    p_fidget: float = 0.4
    sigma: float = 0.12
    merge_m: float = 0.5
    p_ghost: float = 0.02
    p_static_hit: float = 0.98
    p_static_fp: float = 0.003


class Simulator:
    def __init__(self, apt: Apartment, scenario: dict = DEFAULT_SCENARIO, seed: int = 1,
                 model: SensorModel | None = None):
        self.apt = apt
        self.sc = scenario
        self.rng = np.random.default_rng(seed)
        np.random.seed(seed)
        self.m = model or SensorModel()
        self.persons = [Person(**p) for p in scenario["persons"]]
        self.engine = Engine(apt)
        s = apt.raw.get("sensor", {})
        self.max_range = float(s.get("max_range_m", 6.0))
        self.half_fov = math.radians(float(s.get("half_fov_deg", 60)))
        self.history = []   # (t, truth_occ, est_occ, lights, persons_pos, tracks)

    def _p_detect(self, pr: Person, t: float) -> float:
        if pr.vel > 0.15:
            return self.m.p_move
        if pr.fidget:
            return self.m.p_fidget
        still = t - (pr.still_since if pr.still_since is not None else t)
        return self.m.p_still_early if still < self.m.still_fade_s else self.m.p_still_late

    def _frame(self, node, t):
        room = self.apt.rooms[node.room]
        vis = []
        for pr in self.persons:
            p = tuple(pr.pos)
            if dist_point_polygon(p, room.polygon) > 0.15:
                continue
            if not in_fov(p[0], p[1], node.pose, self.max_range, self.half_fov):
                continue
            vis.append(pr)
        dets = []
        for pr in vis:
            if self.rng.random() < self._p_detect(pr, t):
                dets.append(pr.pos + self.rng.normal(0, self.m.sigma, 2))
        # fusion des cibles proches (résolution distance/angle limitée)
        merged = []
        for d in dets:
            for k, m in enumerate(merged):
                if np.hypot(*(d - m)) < self.m.merge_m:
                    merged[k] = (m + d) / 2
                    break
            else:
                merged.append(d)
        if self.rng.random() < self.m.p_ghost:
            xs = [q[0] for q in room.polygon]
            ys = [q[1] for q in room.polygon]
            g = np.array([self.rng.uniform(min(xs), max(xs)), self.rng.uniform(min(ys), max(ys))])
            if in_fov(g[0], g[1], node.pose, self.max_range, self.half_fov):
                merged.append(g)
        merged.sort(key=lambda d: np.hypot(d[0] - node.pose[0], d[1] - node.pose[1]))
        targets = []
        for d in merged[:3]:
            xs, ys = world_to_sensor(d[0], d[1], node.pose)
            targets.append((int(xs * 1000), int(ys * 1000), 0))
        # LD2410C : présence statique dans la pièce
        occupied = any(self.apt.room_of(tuple(pr.pos)) == node.room for pr in self.persons)
        p = self.m.p_static_hit if occupied else self.m.p_static_fp
        st = {"state": 2 if self.rng.random() < p else 0}
        return targets, st

    def run(self, verbose: bool = False) -> dict:
        nodes = list(self.apt.nodes.values())
        n_steps = int(self.sc["duration"] / DT)
        for k in range(n_steps):
            t = k * DT
            for pr in self.persons:
                pr.step(t, DT)
            for i, node in enumerate(nodes):
                tn = t + 0.1 * DT * i / max(1, len(nodes))  # trames décalées entre nœuds
                tg, st = self._frame(node, tn)
                self.engine.ingest_frame(node.id, tn, tg, st)
            self.engine.step(t + DT * 0.99)
            truth = {r: 0 for r in self.apt.rooms}
            for pr in self.persons:
                r = self.apt.room_of(tuple(pr.pos))
                if r:
                    truth[r] += 1
            snap = self.engine.snapshot()
            self.history.append((t, truth, snap["occupancy"], snap["lights"],
                                 [tuple(pr.pos) for pr in self.persons], snap["tracks"]))
        if verbose:
            for e in self.engine.event_log:
                print(e)
        return self.metrics()

    def metrics(self) -> dict:
        off_delay = self.engine.logic.off_delay
        rooms = list(self.apt.rooms)
        dark = {r: 0.0 for r in rooms}
        wasted = {r: 0.0 for r in rooms}
        occ_since = {r: None for r in rooms}
        empty_since = {r: None for r in rooms}
        count_ok = 0
        total = 0
        for t, truth, est, lights, _, _ in self.history:
            for r in rooms:
                if truth[r] > 0:
                    occ_since[r] = occ_since[r] if occ_since[r] is not None else t
                    empty_since[r] = None
                    if not lights[r] and t - occ_since[r] > 1.0:
                        dark[r] += DT
                else:
                    empty_since[r] = empty_since[r] if empty_since[r] is not None else t
                    occ_since[r] = None
                    if lights[r] and t - empty_since[r] > off_delay + 3.0:
                        wasted[r] += DT
                total += 1
                count_ok += int(truth[r] == est[r])
        # continuité d'identité : piste la plus proche de chaque personne
        switches = {}
        for pi, pr in enumerate(self.persons):
            last, sw = None, 0
            for _, _, _, _, ppos, tracks in self.history:
                if self.apt.room_of(ppos[pi]) is None:
                    last = None
                    continue
                best = min(tracks, key=lambda tr: math.hypot(tr["x"] - ppos[pi][0], tr["y"] - ppos[pi][1]),
                           default=None)
                if best is None or math.hypot(best["x"] - ppos[pi][0], best["y"] - ppos[pi][1]) > 1.0:
                    continue
                if last is not None and best["id"] != last:
                    sw += 1
                last = best["id"]
            switches[pr.name] = sw
        return {
            "durée_s": self.sc["duration"],
            "comptage_exact_%": round(100.0 * count_ok / max(1, total), 2),
            "noir_avec_occupant_s": {r: round(v, 1) for r, v in dark.items()},
            "allumé_pièce_vide_s": {r: round(v, 1) for r, v in wasted.items()},
            "changements_identité": switches,
            "détections_rejetées_hors_pièce": self.engine.rejected,
        }

    def plot(self, path: str) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon, Wedge

        fig, (ax, ax2) = plt.subplots(1, 2, figsize=(15, 6.5), gridspec_kw={"width_ratios": [1.1, 1]})
        for r in self.apt.rooms.values():
            ax.add_patch(Polygon(r.polygon, closed=True, fill=False, lw=1.5, ec="#444"))
            cx = sum(p[0] for p in r.polygon) / len(r.polygon)
            cy = sum(p[1] for p in r.polygon) / len(r.polygon)
            ax.text(cx, cy, r.name, ha="center", va="center", color="#999", fontsize=11)
        for d in self.apt.doors:
            ax.plot([d.a[0], d.b[0]], [d.a[1], d.b[1]], color="#e8a33d", lw=5, solid_capstyle="butt")
        for n in self.apt.nodes.values():
            th = math.degrees(n.pose[2])
            ax.add_patch(Wedge(n.pose[:2], self.max_range, th - math.degrees(self.half_fov),
                               th + math.degrees(self.half_fov), alpha=0.04, color="#2a6fdb"))
            ax.plot(*n.pose[:2], "s", color="#2a6fdb", ms=7)
        cols = ["#d62728", "#2ca02c", "#9467bd", "#8c564b", "#17becf"]
        for pi, pr in enumerate(self.persons):
            xs = [h[4][pi][0] for h in self.history]
            ys = [h[4][pi][1] for h in self.history]
            ax.plot(xs, ys, "-", color="#bbb", lw=3, zorder=1)
        seen = {}
        for _, _, _, _, _, tracks in self.history[::3]:
            for tr in tracks:
                seen.setdefault(tr["id"], []).append((tr["x"], tr["y"]))
        for k, (tid, pts) in enumerate(sorted(seen.items())):
            a = np.array(pts)
            ax.plot(a[:, 0], a[:, 1], ".", ms=2.5, color=cols[k % len(cols)], zorder=2)
            ax.text(a[0, 0], a[0, 1], f"#{tid}", color=cols[k % len(cols)], fontsize=8)
        ax.set_aspect("equal")
        ax.set_xlim(-0.5, 10.5)
        ax.set_ylim(-0.5, 9.5)
        ax.set_title("Trajets réels (gris) et pistes estimées (couleur). ■ = nœud radar")

        rooms = list(self.apt.rooms)
        t = np.array([h[0] for h in self.history])
        for i, r in enumerate(rooms):
            tru = np.array([h[1][r] for h in self.history])
            est = np.array([h[2][r] for h in self.history])
            lig = np.array([h[3][r] for h in self.history])
            base = i * 4
            ax2.fill_between(t, base, base + 0.6 * lig, color="#f5d76e", step="post", lw=0)
            ax2.step(t, base + tru, where="post", color="#444", lw=1.5)
            ax2.step(t, base + est, where="post", color="#d62728", lw=1, ls="--")
        ax2.set_yticks([i * 4 + 1 for i in range(len(rooms))], rooms)
        ax2.set_xlabel("temps (s)")
        ax2.set_title("Occupation réelle (noir), estimée (rouge), lumière (jaune)")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
