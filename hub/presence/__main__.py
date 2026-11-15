"""Point d'entrée : python -m presence <commande>.

  sim        rejoue un scénario simulé et affiche les métriques
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


if __name__ == "__main__":
    raise SystemExit(main())
