"""Entrées/sorties MQTT : trames des ESP32, commandes Zigbee2MQTT, état publié.

Format publié par chaque ESP32 sur radar/<node_id>/frame (≈10 Hz) :
  {"seq": 1234, "t": <millis>, "tg": [[x_mm, y_mm, v_cms], ...],
   "st": {"state": 2, "mov": 120, "sta": 230, "e": 45}}
"st" est absent si le nœud n'a pas de LD2410C.

L'horodatage utilisé est celui de réception sur le hub (horloge unique,
pas de synchronisation à faire). La gigue Wi-Fi + broker est de l'ordre de
quelques dizaines de ms ; c'est faible devant 1/10 Hz.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from .alerts import mqtt_sink, ntfy_sink
from .engine import Engine
from .web import Dashboard

log = logging.getLogger(__name__)


def _client(cfg: dict, cid: str):
    import paho.mqtt.client as mqtt
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
    except AttributeError:  # paho 1.x
        c = mqtt.Client(client_id=cid)
    if cfg.get("username"):
        c.username_pw_set(cfg["username"], cfg.get("password") or None)
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    return c


class ZigbeeLights:
    """Commande des lampes via Zigbee2MQTT (même convention que human-sensing)."""

    def __init__(self, client, base_topic: str = "zigbee2mqtt", dry_run: bool = False):
        self.client, self.base, self.dry_run = client, base_topic.rstrip("/"), dry_run

    def set(self, lamp: str, on: bool) -> None:
        payload = json.dumps({"state": "ON" if on else "OFF"})
        topic = f"{self.base}/{lamp}/set"
        log.info("lumière %s -> %s", lamp, "ON" if on else "OFF")
        if not self.dry_run and self.client is not None:
            self.client.publish(topic, payload, qos=1)


class PrintLights:
    def set(self, lamp: str, on: bool) -> None:
        log.info("[dry-run] %s -> %s", lamp, "ON" if on else "OFF")


def run(apt, dry_run: bool = False, rate_hz: float = 10.0) -> None:
    mcfg = apt.raw.get("mqtt", {})
    radar_topic = mcfg.get("radar_topic", "radar").rstrip("/")
    state_topic = mcfg.get("state_topic", "presence").rstrip("/")
    client = _client(mcfg, "radar-presence-hub")
    lights = PrintLights() if dry_run else ZigbeeLights(client, apt.raw.get("zigbee", {}).get("base_topic", "zigbee2mqtt"))
    eng = Engine(apt, lights)
    lock = threading.Lock()
    t0 = time.monotonic()

    # Alertes : MQTT toujours, téléphone (ntfy) si configuré
    eng.alerts.sinks.append(mqtt_sink(client, f"{state_topic}/alerts"))
    ntfy = apt.raw.get("alerts", {}).get("ntfy_url")
    if ntfy:
        eng.alerts.sinks.append(ntfy_sink(ntfy))
    port = apt.raw.get("web", {}).get("port")
    if port:
        Dashboard(apt, eng, lock, lambda: time.monotonic() - t0, int(port)).start()

    def on_connect(c, *_):
        c.subscribe(f"{radar_topic}/+/frame", qos=0)
        log.info("abonné à %s/+/frame", radar_topic)

    def on_message(c, _u, msg):
        try:
            node_id = msg.topic.split("/")[-2]
            d = json.loads(msg.payload)
        except (ValueError, IndexError):
            return
        with lock:
            eng.ingest_frame(node_id, time.monotonic() - t0, d.get("tg", []), d.get("st"))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(mcfg.get("host", "127.0.0.1"), int(mcfg.get("port", 1883)))
    client.loop_start()

    period = 1.0 / rate_hz
    last_pub = 0.0
    try:
        while True:
            time.sleep(period)
            now = time.monotonic() - t0
            with lock:
                eng.step(now)
                snap = eng.snapshot()
                evs = eng.event_log
                eng.event_log = []
            for e in evs:
                client.publish(f"{state_topic}/events", json.dumps(e.__dict__, default=str))
            if now - last_pub >= 1.0:
                last_pub = now
                client.publish(f"{state_topic}/tracks", json.dumps(snap["tracks"]))
                for room, n in snap["occupancy"].items():
                    client.publish(f"{state_topic}/rooms/{room}",
                                   json.dumps({"occupancy": n, "light": snap["lights"][room]}), retain=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
