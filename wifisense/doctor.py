"""Diagnostic : vérifier chaque maillon avant de perdre du temps à calibrer.

Une session de calibrage coûte cinq minutes et impose de sortir de chez soi. La
découvrir ratée après coup parce qu'un nœud n'émettait pas est le pire scénario.
Ce module vérifie donc, dans l'ordre du flux : dépendances → capteurs → données
qui arrivent réellement → broker → modèle.
"""

from __future__ import annotations

import importlib
import os
import socket
import time

OK, WARN, FAIL, INFO = "OK", "ATTENTION", "ÉCHEC", "info"
MARK = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status, label, detail=""):
        self.rows.append((status, label, detail))
        print(f"  [{MARK[status]}] {label}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)


def check_deps(r: Report) -> None:
    print("\n── Dépendances ──")
    for mod, why in [("numpy", "obligatoire"), ("scipy", "obligatoire"),
                     ("yaml", "obligatoire")]:
        try:
            m = importlib.import_module(mod)
            r.add(OK, mod, getattr(m, "__version__", ""))
        except ImportError:
            r.add(FAIL, mod, f"absent ({why}) — relance ./install.sh")
    try:
        importlib.import_module("paho.mqtt")
        r.add(OK, "paho-mqtt")
    except ImportError:
        r.add(WARN, "paho-mqtt",
              "absent : la détection tourne, les lampes ne seront pas pilotées")


def check_sdr(r: Report, cfg: dict) -> None:
    print("\n── LimeSDR ──")
    try:
        import SoapySDR
    except ImportError:
        r.add(FAIL, "SoapySDR", "absent — relance ./install.sh --sdr")
        return
    r.add(OK, "SoapySDR", str(SoapySDR.getAPIVersion()))
    devs = SoapySDR.Device.enumerate()
    if not devs:
        r.add(FAIL, "périphérique", "aucun SDR détecté (câble USB 3.0 ?)")
        return
    for d in devs:
        info = dict(d)
        r.add(OK, "périphérique",
              f"{info.get('label') or info.get('driver')}")
    f = float(cfg["source"].get("frequency_hz", 0))
    if f > 3.5e9:
        r.add(WARN, "fréquence",
              f"{f/1e9:.2f} GHz : au-delà de la portée d'une LimeSDR Mini (3,5 GHz)")


def check_csi(r: Report, cfg: dict, seconds: float) -> None:
    print(f"\n── Nœuds CSI (écoute {seconds:.0f} s) ──")
    port = int(cfg["source"].get("udp_port", 5566))
    expected = sorted({int(z["channel"]) for z in cfg["zones"].values()
                       if z.get("enabled", True)})

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((cfg["source"].get("udp_host", "0.0.0.0"), port))
    except OSError as exc:
        r.add(FAIL, f"port UDP {port}", f"{exc} (le démon tourne déjà ?)")
        return
    sock.settimeout(0.5)

    from .sources.esp32csi import HEADER, MAGIC

    counts: dict[int, int] = {}
    bad = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if len(data) < HEADER.size:
            bad += 1
            continue
        magic, node, *_ = HEADER.unpack_from(data)
        if magic != MAGIC:
            bad += 1
            continue
        counts[node] = counts.get(node, 0) + 1
    sock.close()

    if not counts:
        r.add(FAIL, f"port UDP {port}",
              "aucune trame CSI reçue — nœuds alimentés ? bon port configuré ?")
        return
    for node in sorted(counts):
        rate = counts[node] / seconds
        tag = f"nœud {node}"
        if node not in expected:
            r.add(WARN, tag, f"{rate:.0f} trames/s mais absent de la config")
        elif rate < 5:
            r.add(WARN, tag, f"{rate:.0f} trames/s — trop peu, active le "
                             f"générateur de trafic")
        else:
            r.add(OK, tag, f"{rate:.0f} trames/s")
    for node in expected:
        if node not in counts:
            r.add(FAIL, f"nœud {node}", "déclaré en config mais silencieux")
    if bad:
        r.add(WARN, "trames rejetées", f"{bad} (autre trafic sur ce port ?)")


def check_mqtt(r: Report, cfg: dict, seconds: float = 5.0) -> None:
    print("\n── Zigbee2MQTT ──")
    m = cfg["mqtt"]
    host, port = m.get("host", "127.0.0.1"), int(m.get("port", 1883))
    try:
        with socket.create_connection((host, port), timeout=3):
            r.add(OK, "broker joignable", f"{host}:{port}")
    except OSError as exc:
        r.add(WARN, "broker injoignable", f"{host}:{port} — {exc}")
        return

    try:
        import paho.mqtt.client as mqtt_lib
    except ImportError:
        return

    base = m.get("base_topic", "zigbee2mqtt").rstrip("/")
    seen: dict[str, object] = {}

    def on_connect(c, u, f, rc, *a):
        c.subscribe(f"{base}/bridge/state")
        c.subscribe(f"{base}/bridge/devices")

    def on_message(c, u, msg):
        seen[msg.topic] = msg.payload

    try:
        cl = mqtt_lib.Client(mqtt_lib.CallbackAPIVersion.VERSION1)
    except AttributeError:
        cl = mqtt_lib.Client()
    if m.get("username"):
        cl.username_pw_set(m["username"], m.get("password") or None)
    cl.on_connect, cl.on_message = on_connect, on_message
    try:
        cl.connect(host, port, 10)
    except OSError as exc:
        r.add(WARN, "connexion MQTT", str(exc))
        return
    cl.loop_start()
    time.sleep(seconds)
    cl.loop_stop()
    cl.disconnect()

    if f"{base}/bridge/state" not in seen:
        r.add(WARN, "pont Zigbee2MQTT",
              f"aucun message sur {base}/bridge/state — base_topic correct ?")
        return
    r.add(OK, "pont Zigbee2MQTT", "en ligne")

    raw = seen.get(f"{base}/bridge/devices")
    if not raw:
        return
    import json

    try:
        devices = {d.get("friendly_name") for d in json.loads(raw)}
    except (json.JSONDecodeError, TypeError, AttributeError):
        return
    wanted = {n for z in cfg["zones"].values() for n in z.get("lights", [])}
    missing = sorted(wanted - devices)
    if missing:
        r.add(FAIL, "lampes introuvables", ", ".join(missing))
    else:
        r.add(OK, "lampes", f"{len(wanted)} trouvée(s) sur le pont")


def check_model(r: Report, cfg: dict) -> None:
    print("\n── Calibrage ──")
    from .calibrate import dataset_load

    needs_clf = any(z.get("detector") == "classifier"
                    for z in cfg["zones"].values())
    ds = cfg["classifier"]["dataset_path"]
    data = dataset_load(ds)
    if not needs_clf:
        r.add(INFO, "mode scalaire", "pas de classifieur requis")
    elif not data:
        r.add(WARN, "jeu de données", f"{ds} absent — lance les sessions calibrate")
    else:
        from .localizer import parse_label

        covered = {rm for k in data for rm in parse_label(k)}
        missing = sorted(set(cfg["zones"]) - covered)
        detail = f"{len(data)} session(s), {sum(len(v) for v in data.values())} vecteurs"
        if missing:
            r.add(WARN, "jeu de données", f"{detail} ; manque : {', '.join(missing)}")
        else:
            r.add(OK, "jeu de données", detail)

    path = cfg["classifier"]["model_path"]
    if not needs_clf:
        return
    if not os.path.exists(path):
        r.add(WARN, "modèle", f"{path} absent — lance : wifisense fit")
        return
    from .localizer import RoomClassifier

    clf = RoomClassifier.load(path)
    worst = min(clf.fisher.values()) if clf.fisher else 0.0
    status = OK if worst >= 2 else (WARN if worst >= 1 else FAIL)
    r.add(status, "modèle",
          f"{len(clf.rooms)} pièces, pire ratio de Fisher {worst:.1f}"
          + ("" if worst >= 1 else " — déplace le nœud concerné"))


def run(cfg: dict, listen_s: float = 10.0) -> int:
    print("\n" + "=" * 66)
    print("  wifisense doctor")
    print("=" * 66)
    r = Report()
    check_deps(r)

    kind = cfg["source"].get("type")
    if kind == "limesdr":
        check_sdr(r, cfg)
    elif kind == "esp32csi":
        check_csi(r, cfg, listen_s)
    else:
        print("\n── Source ──")
        r.add(INFO, "simulateur", "aucun matériel à vérifier")

    check_mqtt(r, cfg)
    check_model(r, cfg)

    n_fail = sum(1 for s, _, _ in r.rows if s == FAIL)
    n_warn = sum(1 for s, _, _ in r.rows if s == WARN)
    print("\n" + "-" * 66)
    if n_fail:
        print(f"  {n_fail} problème(s) bloquant(s), {n_warn} avertissement(s).")
    elif n_warn:
        print(f"  Aucun blocage, {n_warn} avertissement(s) à regarder.")
    else:
        print("  Tout est en place.")
    print("-" * 66 + "\n")
    return 1 if n_fail else 0
