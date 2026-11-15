"""Point d'entrée : python -m presence <commande>.

  sim        rejoue un scénario simulé et affiche les métriques
  demo       simulation en temps réel + tableau de bord web (sans matériel)
  run        suivi en direct depuis MQTT (ESP32) vers Zigbee2MQTT
  serial     lit un radar branché en USB-UART (TP protocole, sans ESP32)
  calibrate  estime la pose d'un nœud à partir de points mesurés
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
from pathlib import Path

from . import config

HERE = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="presence")
    ap.add_argument("--config", default=str(HERE / "config.example.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sim")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--plot", default=None, help="chemin d'un PNG de synthèse")
    s.add_argument("--events", action="store_true", help="affiche le journal d'événements")

    d = sub.add_parser("demo")
    d.add_argument("--speed", type=float, default=5.0, help="accélération du temps (défaut ×5)")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--sans-pannes", action="store_true", help="pas de panne simulée")
    d.add_argument("--seed", type=int, default=1)

    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="n'envoie aucune commande de lumière")

    se = sub.add_parser("serial")
    se.add_argument("--port", required=True)
    se.add_argument("--sensor", choices=["ld2450", "ld2410"], default="ld2450")

    c = sub.add_parser("calibrate")
    c.add_argument("points", help="CSV : xs_mm,ys_mm,x_monde_m,y_monde_m")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    apt = config.load(a.config)

    if a.cmd == "sim":
        from .sim import Simulator
        sim = Simulator(apt, seed=a.seed)
        m = sim.run(verbose=a.events)
        print(json.dumps(m, ensure_ascii=False, indent=2))
        if a.plot:
            sim.plot(a.plot)
            print(f"figure : {a.plot}")
    elif a.cmd == "demo":
        _demo(apt, a)
    elif a.cmd == "run":
        from .mqtt_io import run
        run(apt, dry_run=a.dry_run)
    elif a.cmd == "serial":
        import serial  # pyserial
        from .protocol import LD2410Parser, LD2450Parser
        p = LD2450Parser() if a.sensor == "ld2450" else LD2410Parser()
        with serial.Serial(a.port, 256000, timeout=0.1) as ser:
            while True:
                for fr in p.feed(ser.read(256)):
                    print(fr)
    elif a.cmd == "calibrate":
        from .geometry import fit_pose
        S, W = [], []
        with open(a.points, newline="") as f:
            for row in csv.reader(f):
                if not row or row[0].startswith("#"):
                    continue
                xs, ys, xw, yw = map(float, row[:4])
                S.append((xs / 1000, ys / 1000))
                W.append((xw, yw))
        x, y, th, rmse = fit_pose(S, W)
        print(f"pose: [{x:.3f}, {y:.3f}, {math.degrees(th):.1f}]   rmse = {rmse*100:.1f} cm")
    return 0


def _demo(apt, a) -> None:
    """Simulation en temps réel servie par le tableau de bord.

    Pannes simulées par défaut (temps simulé) :
      60–240 s   araignée sur le capteur de la salle de bain
      300–340 s  coupure du nœud de la cuisine
    """
    import threading
    import time

    from .sim import DT, DEFAULT_SCENARIO, SensorModel, Simulator
    from .web import Dashboard

    faults = [] if a.sans_pannes else [
        {"type": "araignee", "node": "n-sdb", "t0": 60, "t1": 240},
        {"type": "coupure", "node": "n-cuisine", "t0": 300, "t1": 340},
    ]
    faults = [f for f in faults if f["node"] in apt.nodes]
    sim = Simulator(apt, DEFAULT_SCENARIO, seed=a.seed, model=SensorModel(faults=faults))
    lock = threading.Lock()
    k = 0

    def truth():
        return [list(map(float, pr.pos)) for pr in sim.persons if apt.room_of(tuple(pr.pos))]

    Dashboard(apt, sim.engine, lock, lambda: k * DT, a.port, truth=truth).start()
    print(f"démo : http://localhost:{a.port}  (temps ×{a.speed:g}, Ctrl+C pour arrêter)")
    try:
        while True:  # après le scénario (25 min simulées), la simulation continue sans mouvement
            with lock:
                sim.tick(k)
                k += 1
            time.sleep(DT / a.speed)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
