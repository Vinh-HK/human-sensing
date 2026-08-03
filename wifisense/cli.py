"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import time

import numpy as np

from . import calibrate as cal
from .config import load_calibration, load_config, save_calibration
from .engine import Engine
from .localizer import RoomClassifier, canonical, parse_label
from .sources import build_source

log = logging.getLogger("wifisense")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _fmt(v, spec="7.2f"):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "    ---"
    return format(v, spec)


# --------------------------------------------------------------------- run


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    calib = load_calibration(args.calibration)
    _setup_logging(args.log_level or cfg["log_level"])

    for name in cfg["zones"]:
        if name not in (calib.get("zones") or {}):
            log.warning(
                "Zone %r non calibrée : baseline = 0 dB, les seuils seront faux. "
                "Lance d'abord : wifisense calibrate --label empty",
                name,
            )

    engine = Engine(cfg, calib, dry_run=args.dry_run)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: engine.stop())

    last = [0.0]

    def on_update(t, zones, diag):
        if t - last[0] < 10.0:
            return
        last[0] = t
        parts = [
            f"{z.name}={'ON ' if z.state else 'off'}"
            f"({_fmt(z.last.score_db if z.last else None, '.1f')}dB)"
            for z in zones
        ]
        log.info("état : %s", "  ".join(parts))

    engine.run(on_update=on_update)
    return 0


# ----------------------------------------------------------------- monitor


def cmd_monitor(args) -> int:
    cfg = load_config(args.config)
    calib = load_calibration(args.calibration)
    _setup_logging(args.log_level or "WARNING")

    engine = Engine(cfg, calib, dry_run=True, control_lights=False)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: engine.stop())

    state = {"t": 0.0}

    def on_update(t, zones, diag):
        if t - state["t"] < 0.5:
            return
        state["t"] = t
        rows = [
            f"  {'zone':<12}{'type':<11}{'score':>9}{'seuil':>9}"
            f"{'det':>5}{'lampe':>7}{'reste':>8}   info"
        ]
        for z in zones:
            d = z.last
            thr = z.detector.baseline_db + z.detector.threshold_db
            info = ""
            if d and d.info:
                info = " ".join(
                    f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                    for k, v in d.info.items()
                )
            rows.append(
                f"  {z.name:<12}{z.cfg['detector']:<11}"
                f"{_fmt(d.score_db if d else None, '9.2f')}{thr:9.2f}"
                f"{('  X' if d and d.detected else '  .'):>5}"
                f"{('  ON' if z.state else ' off'):>7}"
                f"{z.presence.remaining_s(time.monotonic()):8.0f}   {info}"
            )
        duty = " ".join(
            f"ch{c}:{v.get('duty', 0) * 100:.0f}%"
            for c, v in (diag.get("channels") or {}).items()
        )
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.write(f"  wifisense monitor — t={t:6.1f}s   trafic {duty}\n\n")
        sys.stdout.write("\n".join(rows) + "\n")
        for a in (diag.get("alerts") or [])[-3:]:
            sys.stdout.write(f"\n  ⚠ [{a.severity}] {a.message}\n")
        sys.stdout.flush()

    try:
        engine.run(duration_s=args.duration, on_update=on_update)
    except StopIteration:
        pass
    return 0


# --------------------------------------------------------------- calibrate


def cmd_calibrate(args) -> int:
    cfg = load_config(args.config)
    calib = load_calibration(args.calibration)
    _setup_logging(args.log_level or "WARNING")

    rooms = parse_label(args.label)
    unknown = rooms - set(cfg["zones"])
    if unknown:
        print(f"Pièces inconnues : {sorted(unknown)}. "
              f"Zones déclarées : {sorted(cfg['zones'])}", file=sys.stderr)
        return 1

    if args.label == "empty":
        msg = ("QUITTEZ le logement — TOUS les occupants. C'est la mesure la plus "
               "importante du lot.")
    elif args.label == "machine":
        msg = "QUITTEZ le logement, mais LANCEZ la machine (lave-linge/vaisselle)."
    else:
        who = " et ".join(sorted(rooms))
        msg = (f"INSTALLEZ-VOUS dans : {who}. Une personne par pièce, "
               f"comportement normal. Les autres pièces doivent rester vides.")

    print(f"\n>>> {msg}")
    print(f">>> Mesure de {args.duration:.0f} s ; départ dans {args.warmup:.0f} s.\n")
    time.sleep(args.warmup)

    def progress(t, total, zones, diag):
        sys.stdout.write(f"\r  {t:6.1f} / {total:.0f} s")
        sys.stdout.flush()

    scores, feats = cal.collect(cfg, calib, args.duration, progress)
    print()
    if not scores and feats.size == 0:
        print("Rien collecté : durée trop courte pour remplir les fenêtres "
              "glissantes (le détecteur respiration demande au moins 90 s).",
              file=sys.stderr)
        return 1

    # Volet scalaire : baselines des détecteurs par zone.
    scalar_label = args.label if args.label in cal.SCALAR_LABELS else "occupied"
    if scores:
        stats = {z: cal.summarize(a) for z, a in scores.items()}
        if scalar_label == "occupied":
            # Une session « cuisine,salon » ne dit rien des zones inoccupées.
            stats = {z: s for z, s in stats.items() if z in rooms}
        if stats:
            calib = cal.merge(calib, scalar_label, stats)
            print(cal.report(calib))

    # Volet classifieur : vecteurs multi-nœuds étiquetés.
    ds_path = cfg["classifier"]["dataset_path"]
    if args.dry_run:
        print("(--dry-run : ni calibrage ni jeu de données écrits)")
        return 0

    save_calibration(args.calibration, calib)
    print(f"Calibrage scalaire écrit dans {args.calibration}")
    if feats.size:
        key = canonical(rooms)
        data = cal.dataset_append(ds_path, key, feats, replace=not args.append)
        verb = "ajoutés à" if args.append else "enregistrés sous"
        print(f"{len(feats)} vecteurs {verb} « {key} » "
              f"({len(data[key])} au total) dans {ds_path}")
        print(f"Sessions présentes : {', '.join(sorted(data))}")
        missing = set(cfg["zones"]) - {r for k in data for r in parse_label(k)}
        classif = {n for n, z in cfg["zones"].items() if z["detector"] == "classifier"}
        todo = sorted(missing & classif)
        if todo:
            print(f"Manque encore : {', '.join(todo)}")
        elif classif:
            print("Toutes les pièces sont couvertes → lance : wifisense fit")
    return 0


def cmd_setup(args) -> int:
    from . import wizard

    return wizard.run(args.config, assume_defaults=args.defaults, force=args.force)


def cmd_doctor(args) -> int:
    from . import doctor

    _setup_logging(args.log_level or "WARNING")
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"{args.config} introuvable. Lance d'abord : wifisense setup",
              file=sys.stderr)
        return 1
    return doctor.run(cfg, listen_s=args.listen)


def cmd_fit(args) -> int:
    cfg = load_config(args.config)
    _setup_logging(args.log_level or "INFO")

    ccfg = cfg["classifier"]
    data = cal.dataset_load(ccfg["dataset_path"])
    if not data:
        print(f"Jeu de données vide ou absent : {ccfg['dataset_path']}\n"
              f"Lance d'abord des sessions `calibrate --label ...`", file=sys.stderr)
        return 1

    print("\nSessions disponibles")
    for k, v in sorted(data.items()):
        print(f"  {k:<24} {len(v):5d} vecteurs")

    dim = next(iter(data.values())).shape[1]
    names = [f"f{i}" for i in range(dim)]
    try:
        clf = RoomClassifier.fit(data, names, reg_alpha=args.reg)
    except ValueError as exc:
        print(f"\nEntraînement impossible : {exc}", file=sys.stderr)
        return 1

    print(clf.report())
    clf.save(ccfg["model_path"])
    print(f"Modèle écrit dans {ccfg['model_path']}")
    print("Vérifie en direct avant de piloter les lampes : wifisense monitor")
    return 0


# ------------------------------------------------------------ record/replay


def cmd_record(args) -> int:
    cfg = load_config(args.config)
    _setup_logging(args.log_level or "INFO")

    channels = sorted({int(z["channel"]) for z in cfg["zones"].values() if z["enabled"]})
    src = build_source(cfg["source"], channels)
    acc: dict[str, list[np.ndarray]] = {}
    stop = {"v": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.__setitem__("v", True))

    src.start()
    t0 = time.monotonic()
    try:
        while not stop["v"]:
            data, _ = src.read()
            for ch, streams in data.items():
                for name, arr in streams.items():
                    acc.setdefault(f"{ch}__{name}", []).append(np.asarray(arr, np.float32))
            el = time.monotonic() - t0
            sys.stdout.write(f"\r  enregistrement… {el:6.1f} s")
            sys.stdout.flush()
            if args.duration and el >= args.duration:
                break
    finally:
        src.stop()
    print()

    out = {k: np.concatenate(v) for k, v in acc.items() if v}
    out["slow_rate_hz"] = np.asarray(src.slow_rate_hz)
    np.savez_compressed(args.out, **out)
    n = max((len(v) for k, v in out.items() if k != "slow_rate_hz"), default=0)
    print(f"{n} échantillons ({n / src.slow_rate_hz:.0f} s) écrits dans {args.out}")
    return 0


def cmd_replay(args) -> int:
    cfg = load_config(args.config)
    cfg["source"]["type"] = "replay"
    cfg["source"]["replay_path"] = args.path
    calib = load_calibration(args.calibration)
    _setup_logging(args.log_level or "WARNING")

    engine = Engine(cfg, calib, dry_run=True, control_lights=False)
    hist: dict[str, list[float]] = {}

    def on_update(t, zones, diag):
        for z in zones:
            if z.last and math.isfinite(z.last.score_db):
                hist.setdefault(z.name, []).append(z.last.score_db)

    try:
        engine.run(on_update=on_update)
    except StopIteration:
        pass

    print(f"\n  {'zone':<12}{'n':>7}{'p05':>9}{'médiane':>9}{'p95':>9}{'% détecté':>11}")
    for z in engine.zones:
        a = np.asarray(hist.get(z.name, []))
        if a.size == 0:
            print(f"  {z.name:<12}      0")
            continue
        thr = z.detector.baseline_db + z.detector.threshold_db
        print(
            f"  {z.name:<12}{a.size:7d}{np.percentile(a, 5):9.2f}"
            f"{np.median(a):9.2f}{np.percentile(a, 95):9.2f}"
            f"{100.0 * (a > thr).mean():10.1f}%"
        )
    print()
    return 0


# ---------------------------------------------------------------- selftest


def _score_series(source, detector, seconds, hint=False):
    """Retourne (scores finis, dernière Detection) pour `seconds` simulées."""
    n = int(seconds / source.block_s)
    out, last = [], None
    for _ in range(n):
        data, _diag = source.read()
        x = data[0][detector.STREAM]
        if len(x) == 0:
            continue
        d = detector.update(x, presence_hint=hint)
        if d is not None and math.isfinite(d.score_db):
            out.append(d.score_db)
            last = d
    return np.asarray(out), last


def cmd_selftest(args) -> int:
    """Valide la chaîne complète sur des scénarios simulés, sans matériel."""
    from .detectors import build_detector
    from .sources.fake import FakeSource

    _setup_logging(args.log_level or "WARNING")
    base = {
        "slow_rate_hz": 100.0,
        "sample_rate_hz": 200e3,
        "fine_decim": 10,
        "block_s": 1.0,
        "fake_speed": 1000.0,  # > 1 : aucune attente, on tourne au plus vite
        "fake_scenario": "empty",
    }
    fs = base["slow_rate_hz"]
    ok = True

    def check(label, cond, detail):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'OK ' if cond else 'ÉCHEC'}] {label}: {detail}")

    print("\nselftest — simulateur, aucun matériel requis")
    print("-" * 64)

    # 1. Douche : variance de l'enveloppe.
    src = FakeSource({**base, "fake_scenario": "empty"}, [0])
    src.start()
    det = build_detector("shower", fs, {"threshold_db": 6.0})
    empty, _ = _score_series(src, det, 40)
    src.set_scenario(0, "shower")
    shower, _ = _score_series(src, det, 40)
    gap = np.percentile(shower, 5) - np.percentile(empty, 95)
    check("douche", gap > 6.0, f"séparation vide→douche = {gap:.1f} dB (attendu > 6)")

    # 2. Respiration : raie spectrale + stabilité fréquentielle.
    src = FakeSource({**base, "fake_scenario": "empty"}, [0])
    src.start()
    det = build_detector("breathing", fs, {"threshold_db": 8.0})
    empty, _ = _score_series(src, det, 150)
    src.set_scenario(0, "breathing")
    _score_series(src, det, 90)  # remplissage de la fenêtre glissante de 60 s
    breath, last = _score_series(src, det, 120)
    gap = np.percentile(breath, 5) - np.percentile(empty, 95)
    check("respiration", gap > 8.0, f"séparation vide→respiration = {gap:.1f} dB (attendu > 8)")
    bpm = (last.info or {}).get("bpm", 0.0) if last else 0.0
    check("respiration bpm", 10.0 < bpm < 20.0, f"{bpm:.1f} cycles/min (simulé : 15)")

    # 3. Cuisine : la machine seule ne doit pas déclencher, un humain devant la
    #    machine doit déclencher. C'est le test qui valide le remplacement du
    #    filtrage fréquentiel fixe par la soustraction de fond adaptatif.
    src = FakeSource({**base, "fake_scenario": "machine"}, [0])
    src.start()
    det = build_detector("kitchen", fs, {"threshold_db": 6.0})
    _score_series(src, det, 90)  # apprentissage du fond « machine »
    machine, _ = _score_series(src, det, 60)
    src.set_scenario(0, "machine_human")
    human, _ = _score_series(src, det, 60, hint=True)
    check(
        "machine seule",
        np.percentile(machine, 95) < 6.0,
        f"p95 = {np.percentile(machine, 95):.1f} dB (doit rester sous le seuil de 6)",
    )
    gap = np.percentile(human, 50) - np.percentile(machine, 95)
    check("humain devant machine", gap > 6.0, f"séparation = {gap:.1f} dB (attendu > 6)")

    # 4. Classifieur de pièces : c'est le test qui compte pour un logement à
    #    plusieurs occupants. Le couplage cuisine↔salon est volontairement fort
    #    (0,40/0,45) pour simuler un séjour ouvert — le cas difficile.
    from .features import FeatureBank
    from .localizer import RoomClassifier

    rooms = {
        "douche":  [1.00, 0.25, 0.12, 0.08],
        "chambre": [0.22, 1.00, 0.20, 0.15],
        "cuisine": [0.12, 0.18, 1.00, 0.40],
        "salon":   [0.10, 0.14, 0.45, 1.00],
    }
    chans = [0, 1, 2, 3]
    src = FakeSource(
        {**base, "fake_world": {"rooms": rooms, "occupants": [], "machine_node": 2}},
        chans,
    )
    src.start()
    bank = FeatureBank(chans, {c: fs for c in chans}, window_s=8.0, n_bands=6)

    def gather(occ, blocks=95, skip=12):
        src.set_occupants(occ)
        rows = []
        for i in range(blocks):
            data, _ = src.read()
            v = bank.update(data)
            if v is not None and i >= skip:
                rows.append(v)
        return np.asarray(rows)

    samples = {"empty": gather([])}
    for r in rooms:
        samples[r] = gather([r])
    samples["cuisine,salon"] = gather(["cuisine", "salon"])

    clf = RoomClassifier.fit(samples, bank.names)
    worst = min(clf.fisher.values())
    check("classifieur séparabilité", worst >= 2.0,
          f"pire ratio de Fisher = {worst:.1f} (attendu >= 2)")

    def occupancy(occ):
        rows = [clf.scores(v) for v in gather(occ, blocks=45)]
        return {r: float(np.median([x[r] for x in rows])) for r in clf.rooms}

    thr = 4.0
    for label, occ, expect in [
        ("vide", [], set()),
        ("seul en douche", ["douche"], {"douche"}),
        ("cuisine + salon", ["cuisine", "salon"], {"cuisine", "salon"}),
        ("3 invités", ["salon", "salon", "cuisine"], {"salon", "cuisine"}),
    ]:
        med = occupancy(occ)
        got = {r for r, v in med.items() if v > thr}
        check(f"classifieur {label}", got == expect,
              " ".join(f"{r}={med[r]:.1f}" for r in clf.rooms))

    # 5. Moniteur de dérive : il doit rester muet sur un logement inchangé et
    #    signaler un réaménagement. Ici le salon se met à rayonner davantage vers
    #    le nœud cuisine que vers le sien — le cas où le classifieur devient
    #    confortablement faux, que seule la mesure de nouveauté peut attraper.
    from .drift import DriftMonitor

    moved = dict(rooms, salon=[0.10, 0.30, 0.85, 0.55])
    dcfg = {"report_every_s": 1e9, "min_samples": 20}

    def drift_run(room_map, occ, blocks=90):
        src.set_rooms(room_map)
        mon = DriftMonitor(clf, dcfg)
        for _ in range(12):
            bank.update(src.read()[0])
        src.set_occupants(occ)
        for _ in range(blocks):
            v = bank.update(src.read()[0])
            if v is not None:
                mon.update(0.0, v, clf.scores(v))
        return {a.kind for a in mon._report()}

    quiet = drift_run(rooms, ["salon"])
    check("dérive témoin (rien n'a bougé)", "attribution" not in quiet,
          f"alertes = {sorted(quiet) or 'aucune'}")
    moved_alerts = drift_run(moved, ["salon"])
    check("dérive après réaménagement", "attribution" in moved_alerts,
          f"alertes = {sorted(moved_alerts) or 'aucune'}")
    src.set_rooms(rooms)

    print("-" * 64)
    print("selftest :", "TOUT EST BON\n" if ok else "AU MOINS UN ÉCHEC\n")
    return 0 if ok else 1


# ----------------------------------------------------------------- parsing


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="wifisense",
        description="Détection de présence par Wi-Fi passif, pilotage Zigbee2MQTT.",
    )
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-k", "--calibration", default="calibration.yaml")
    p.add_argument("--log-level", default=None)

    # Les mêmes options sont acceptées après la sous-commande : « run -c x.yaml »
    # est le réflexe naturel et échouerait sinon. SUPPRESS est indispensable —
    # sans lui, la valeur par défaut du sous-parseur écraserait celle donnée
    # avant la sous-commande.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS)
    common.add_argument("-k", "--calibration", default=argparse.SUPPRESS)
    common.add_argument("--log-level", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("setup", parents=[common],
                        help="assistant : génère config.yaml par questions")
    st.add_argument("--defaults", action="store_true",
                    help="ne rien demander, prendre les valeurs par défaut")
    st.add_argument("--force", action="store_true", help="écraser une config existante")
    st.set_defaults(func=cmd_setup)

    dc = sub.add_parser("doctor", parents=[common],
                        help="vérifie matériel, nœuds, broker et calibrage")
    dc.add_argument("--listen", type=float, default=10.0,
                    help="durée d'écoute des nœuds CSI, en secondes")
    dc.set_defaults(func=cmd_doctor)

    r = sub.add_parser("run", parents=[common],
                       help="démon : détecte et pilote les lampes")
    r.add_argument("--dry-run", action="store_true", help="n'envoie rien en MQTT")
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("monitor", parents=[common],
                       help="affichage live des scores, sans piloter")
    m.add_argument("--duration", type=float, default=None)
    m.set_defaults(func=cmd_monitor)

    c = sub.add_parser("calibrate", parents=[common],
                       help="mesure les références d'une situation")
    c.add_argument(
        "--label", required=True,
        help="'empty', 'machine', ou une liste de pièces occupées "
             "('salon' ou 'cuisine,salon' pour une session à deux)",
    )
    c.add_argument("--duration", type=float, default=180.0)
    c.add_argument("--warmup", type=float, default=20.0, help="délai pour sortir de la pièce")
    c.add_argument(
        "--append", action="store_true",
        help="compléter la session existante au lieu de la remplacer ; utile "
             "pour couvrir plusieurs agencements d'une même pièce",
    )
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_calibrate)

    rec = sub.add_parser("record", parents=[common],
                         help="enregistre les séries temps-lent (.npz)")
    rec.add_argument("--out", required=True)
    rec.add_argument("--duration", type=float, default=None)
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("replay", parents=[common],
                         help="rejoue un enregistrement pour régler les seuils")
    rep.add_argument("path")
    rep.set_defaults(func=cmd_replay)

    f = sub.add_parser("fit", parents=[common],
                       help="entraîne le classifieur de pièces sur le calibrage")
    f.add_argument("--reg", type=float, default=0.15,
                   help="régularisation de la covariance (monte-la si peu d'exemples)")
    f.set_defaults(func=cmd_fit)

    s = sub.add_parser("selftest", parents=[common],
                       help="valide la chaîne sur données simulées")
    s.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
