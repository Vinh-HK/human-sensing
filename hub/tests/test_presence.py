"""Tests : protocole, géométrie, suivi, scénarios simulés.

Lancer : cd hub && python -m pytest -q
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from presence import config, protocol  # noqa: E402
from presence.geometry import fit_pose, sensor_to_world, world_to_sensor  # noqa: E402
from presence.sim import Simulator  # noqa: E402
from presence.tracker import CONFIRMED, HELD  # noqa: E402

CFG = Path(__file__).resolve().parent.parent / "config.example.yaml"


# ------------------------------------------------------------------ protocole

def test_ld2450_datasheet_example():
    # Exemple du manuel Hi-Link : x = -782 mm, y = 1713 mm, v = -16 cm/s, rés. = 320 mm
    t = bytes.fromhex("0E03B186100040 01".replace(" ", ""))
    frame = protocol.LD2450_HEAD + t + bytes(16) + protocol.LD2450_TAIL
    (tg,) = protocol.decode_ld2450_frame(frame)
    assert (tg.x_mm, tg.y_mm, tg.speed_cms, tg.res_mm) == (-782, 1713, -16, 320)


def test_ld2450_stream_resync():
    f1 = protocol.encode_ld2450_frame([(100, 2000, 5, 360), (-500, 1500, 0, 360)])
    f2 = protocol.encode_ld2450_frame([])
    junk = b"\x00\xAA\xFF\x12"  # faux début d'en-tête
    stream = junk + f1 + f2
    p = protocol.LD2450Parser()
    frames = []
    for k in range(0, len(stream), 7):  # découpage arbitraire, comme une UART
        frames += p.feed(stream[k:k + 7])
    assert len(frames) == 2
    assert [(t.x_mm, t.y_mm) for t in frames[0]] == [(100, 2000), (-500, 1500)]
    assert frames[1] == []


def test_ld2410_report():
    raw = protocol.encode_ld2410_report(2, 0, 0, 230, 45, 230)
    p = protocol.LD2410Parser()
    out = p.feed(b"\x01\x02" + raw[:5]) + p.feed(raw[5:])
    assert len(out) == 1 and out[0].state == 2 and out[0].static_cm == 230 and out[0].present


# ------------------------------------------------------------------ géométrie

def test_frames_roundtrip():
    pose = (1.0, 2.0, math.radians(30))
    for xs, ys in [(0.0, 1.0), (0.5, 2.0), (-1.2, 3.3)]:
        wx, wy = sensor_to_world(xs, ys, pose)
        bx, by = world_to_sensor(wx, wy, pose)
        assert abs(bx - xs) < 1e-9 and abs(by - ys) < 1e-9


def test_boresight_direction():
    # radar en (0,0) qui regarde vers le nord : une cible droit devant à 2 m
    assert sensor_to_world(0, 2, (0, 0, math.pi / 2)) == pytest.approx((0, 2))
    # à droite du radar = vers l'est
    assert sensor_to_world(1, 0, (0, 0, math.pi / 2)) == pytest.approx((1, 0))


def test_fit_pose():
    true = (0.1, 0.1, math.radians(45))
    W = [(1, 1), (2, 3), (4, 1), (3, 3.5), (1.5, 2.5)]
    S = [world_to_sensor(x, y, true) for x, y in W]
    x, y, th, rmse = fit_pose(S, W)
    assert (x, y) == pytest.approx(true[:2], abs=1e-6)
    assert th == pytest.approx(true[2], abs=1e-6)
    assert rmse < 1e-6


# ------------------------------------------------------------------ suivi

def _scenario(persons, duration):
    return dict(duration=duration, persons=persons)


def test_static_person_kept_and_light_stays_on():
    """Le cas qui justifie le projet : quelqu'un d'immobile ne doit pas être « oublié »."""
    apt = config.load(CFG)
    sc = _scenario([dict(name="A", t0=0, start=(2.0, 7.0), script=[("stay", 600)])], 600)
    sim = Simulator(apt, sc, seed=4)
    m = sim.run()
    assert m["noir_avec_occupant_s"]["chambre"] == 0.0
    states = [tr.state for tr in sim.engine.tracker.tracks]
    assert states.count(HELD) + states.count(CONFIRMED) == 1


def test_walk_through_rooms_lights_follow():
    apt = config.load(CFG)
    sc = _scenario([dict(name="A", t0=1, start=(8.6, 4.6), script=[
        ("go", [(7.5, 4.6), (2.5, 4.6), (2.5, 3.4), (2.0, 2.0)]), ("stay", 30, "fidget"),
        ("go", [(3.0, 1.5), (5.0, 1.5), (6.5, 2.0)]), ("stay", 30, "fidget"),
    ])], 90)
    sim = Simulator(apt, sc, seed=2)
    m = sim.run()
    ev = [(e.kind, e.src, e.dst) for e in sim.engine.event_log]
    assert ("entrée", "couloir", "salon") in ev
    assert ("entrée", "salon", "cuisine") in ev
    assert sum(m["noir_avec_occupant_s"].values()) < 2.0
    assert sim.engine.logic.on["cuisine"] and not sim.engine.logic.on["salon"]


def test_leaving_apartment():
    apt = config.load(CFG)
    sc = _scenario([dict(name="A", t0=0, start=(4.0, 4.6), script=[
        ("stay", 3, "fidget"), ("go", [(7.6, 4.6), (8.6, 4.6), (10, 4.6)])])], 30)
    sim = Simulator(apt, sc, seed=3)
    sim.run()
    kinds = [(e.kind, e.dst) for e in sim.engine.event_log]
    assert ("sortie", "exterieur") in kinds
    assert not any(sim.engine.logic.on.values())


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_default_scenario_quality(seed):
    apt = config.load(CFG)
    m = Simulator(apt, seed=seed).run()
    assert m["comptage_exact_%"] > 98.0
    assert sum(m["noir_avec_occupant_s"].values()) < 5.0
    assert sum(m["allumé_pièce_vide_s"].values()) < 60.0


# ------------------------------------------------------------------ alertes

def test_target_glued_to_sensor_is_ignored_and_alerted():
    """Insecte ou araignée sur le boîtier : cible ignorée, alerte levée puis close."""
    from presence.sim import SensorModel
    apt = config.load(CFG)
    sc = _scenario([dict(name="A", t0=0, start=(2.0, 7.0), script=[("stay", 400)])], 400)
    m = SensorModel(faults=[{"type": "araignee", "node": "n-sdb", "t0": 20, "t1": 250}])
    sim = Simulator(apt, sc, seed=5, model=m)
    r = sim.run()
    hist = {a.key: a for a in sim.engine.alerts.history}
    assert "proche:n-sdb" in hist and not hist["proche:n-sdb"].active
    assert "statique:n-sdb" in hist
    # la salle de bain est vide : l'araignée ne doit jamais l'allumer
    assert r["allumé_pièce_vide_s"]["sdb"] == 0.0


def test_silent_node_alert():
    from presence.sim import SensorModel
    apt = config.load(CFG)
    sc = _scenario([dict(name="A", t0=0, start=(2.0, 2.0), script=[("stay", 120, "fidget")])], 120)
    m = SensorModel(faults=[{"type": "coupure", "node": "n-cuisine", "t0": 10, "t1": 60}])
    sim = Simulator(apt, sc, seed=1, model=m)
    sim.run()
    a = {x.key: x for x in sim.engine.alerts.history}["muet:n-cuisine"]
    assert a.level == "erreur" and 24 < a.since < 27 and not a.active


def test_no_false_alert_in_normal_run():
    apt = config.load(CFG)
    r = Simulator(apt, seed=2).run()
    assert r["alertes"] == []


def test_dashboard_api():
    import json
    import threading
    import urllib.request
    from presence.engine import Engine
    from presence.web import Dashboard
    apt = config.load(CFG)
    eng = Engine(apt)
    eng.ingest_frame("n-salon", 0.0, [(0, 2000, 0)], {"state": 1})
    eng.step(0.1)
    d = Dashboard(apt, eng, threading.Lock(), lambda: 0.2, port=0, host="127.0.0.1")
    d.start()
    port = d.httpd.server_address[1]
    try:
        plan = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/plan"))
        state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state"))
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
    finally:
        d.stop()
    assert len(plan["rooms"]) == 5 and len(plan["nodes"]) == 6
    assert set(state) >= {"occupancy", "lights", "tracks", "nodes", "alerts"}
    assert "tableau de bord" in page
