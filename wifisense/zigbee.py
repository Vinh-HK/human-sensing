"""Pilotage des ampoules IKEA via Zigbee2MQTT.

On passe par MQTT et non par l'API HTTP du frontend : le frontend est une
interface d'administration, MQTT est l'interface machine de Zigbee2MQTT et
c'est le seul chemin qui nous notifie aussi des changements d'état.

Topics utilisés :
  <base>/<ampoule>/set   → commande      {"state": "ON", "brightness": 200}
  <base>/<ampoule>       → état rapporté par l'ampoule
  <base>/bridge/state    → santé du pont
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger(__name__)


class ZigbeeLights:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.base = cfg.get("base_topic", "zigbee2mqtt").rstrip("/")
        self.refresh_s = float(cfg.get("refresh_s", 300.0))
        self.override_s = float(cfg.get("override_s", 900.0))

        self._lock = threading.Lock()
        self._desired: dict[str, bool] = {}
        self._last_pub: dict[str, float] = {}
        self._override_until: dict[str, float] = {}
        self._lux: float | None = None
        self._warned_offline = False
        self.bridge_online = False
        self.client = None

    # ------------------------------------------------------------------ MQTT

    def connect(self) -> None:
        if self.dry_run:
            log.info("MQTT en mode dry-run : aucune commande ne sera envoyée")
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.dry_run = True
            log.warning(
                "paho-mqtt absent : passage en dry-run. La détection fonctionne, "
                "les commandes sont seulement journalisées."
            )
            return

        # paho 2.x impose de déclarer la version d'API des callbacks ; paho 1.x
        # ne connaît pas l'argument. On gère les deux sans figer la dépendance.
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=self.cfg.get("client_id", "wifisense"),
            )
        except AttributeError:
            self.client = mqtt.Client(client_id=self.cfg.get("client_id", "wifisense"))

        if self.cfg.get("username"):
            self.client.username_pw_set(
                self.cfg["username"], self.cfg.get("password") or None
            )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

        # connect_async + loop_start : la connexion est tentée (et retentée) par
        # le thread réseau de paho. Un broker absent ou redémarré ne fait donc
        # jamais tomber la détection — c'est volontaire, la moitié « capteur » du
        # système doit pouvoir tourner seule, avant même qu'un coordinateur
        # Zigbee existe.
        host = self.cfg.get("host", "127.0.0.1")
        port = int(self.cfg.get("port", 1883))
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()
        log.info("Connexion MQTT à %s:%s en arrière-plan", host, port)

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc != 0:
            log.error("Connexion MQTT refusée (code %s)", rc)
            return
        log.info("MQTT connecté à %s:%s", self.cfg.get("host"), self.cfg.get("port"))
        client.subscribe(f"{self.base}/bridge/state")
        with self._lock:
            names = list(self._desired)
            # Après une coupure, l'état des ampoules a pu diverger pendant qu'on
            # ne pouvait plus publier. On force une réémission au prochain tour.
            self._last_pub = {n: 0.0 for n in names}
        for name in names:
            client.subscribe(f"{self.base}/{name}")
        if self.cfg.get("lux_topic"):
            client.subscribe(self.cfg["lux_topic"])

    def _on_message(self, client, userdata, msg):
        try:
            self._handle(msg.topic, msg.payload.decode("utf-8", "replace"))
        except Exception:  # un message malformé ne doit jamais tuer le thread
            log.exception("Message MQTT ignoré (%s)", msg.topic)

    def _handle(self, topic: str, payload: str) -> None:
        if topic == f"{self.base}/bridge/state":
            state = payload.strip().strip('"')
            try:
                state = json.loads(payload).get("state", state)
            except (json.JSONDecodeError, AttributeError):
                pass
            self.bridge_online = state == "online"
            return

        if self.cfg.get("lux_topic") and topic == self.cfg["lux_topic"]:
            try:
                data = json.loads(payload)
                self._lux = float(data.get(self.cfg.get("lux_field"), self._lux))
            except (json.JSONDecodeError, TypeError, ValueError):
                try:
                    self._lux = float(payload)
                except ValueError:
                    pass
            return

        if not topic.startswith(f"{self.base}/"):
            return
        name = topic[len(self.base) + 1 :]
        with self._lock:
            if name not in self._desired:
                return
            try:
                reported = json.loads(payload).get("state")
            except (json.JSONDecodeError, AttributeError):
                return
            if reported not in ("ON", "OFF"):
                return
            actual = reported == "ON"
            desired = self._desired[name]
            # L'écho de notre propre commande arrive dans la seconde ; au-delà,
            # une divergence signifie que quelqu'un a touché l'interrupteur ou
            # l'appli. On lui laisse la main plutôt que de lutter contre lui.
            if actual != desired and (time.time() - self._last_pub.get(name, 0.0)) > 5.0:
                self._override_until[name] = time.time() + self.override_s
                self._desired[name] = actual
                log.info(
                    "Commande manuelle détectée sur %s (%s) : automatisation "
                    "suspendue %.0f s",
                    name, reported, self.override_s,
                )

    # ------------------------------------------------------------------ API

    def register(self, names) -> None:
        with self._lock:
            for n in names:
                self._desired.setdefault(n, False)
        if self.client is not None:
            for n in names:
                self.client.subscribe(f"{self.base}/{n}")

    def publish_status(self, topic: str, payload: dict) -> None:
        """Publie un message de service (alertes de dérive, santé)."""
        if not topic:
            return
        body = json.dumps(payload, ensure_ascii=False)
        if self.dry_run or self.client is None:
            log.info("[dry-run] %s <- %s", topic, body)
            return
        self.client.publish(topic, body, qos=1, retain=True)

    def dark_enough(self) -> bool:
        """Garde-fou luminosité. Vrai si pas de capteur configuré."""
        if not self.cfg.get("lux_topic") or self._lux is None:
            return True
        return self._lux < float(self.cfg.get("lux_threshold", 40.0))

    def apply(self, zone_cfg: dict, on: bool) -> None:
        now = time.time()
        for name in zone_cfg.get("lights", []):
            with self._lock:
                if now < self._override_until.get(name, 0.0):
                    continue
                changed = self._desired.get(name) != on
                stale = (now - self._last_pub.get(name, 0.0)) > self.refresh_s
                if not changed and not stale:
                    continue
                if on and changed and not self.dark_enough():
                    continue
                self._desired[name] = on
                self._last_pub[name] = now

            payload = {"state": "ON" if on else "OFF"}
            if zone_cfg.get("transition") is not None:
                payload["transition"] = zone_cfg["transition"]
            if on and zone_cfg.get("brightness") is not None:
                payload["brightness"] = int(zone_cfg["brightness"])

            topic = f"{self.base}/{name}/set"
            if self.dry_run or self.client is None:
                log.info("[dry-run] %s <- %s", topic, json.dumps(payload))
                continue

            info = self.client.publish(topic, json.dumps(payload), qos=1)
            if info.rc != 0:
                # Broker injoignable : on remet le compteur à zéro pour que la
                # condition « stale » relance l'envoi au tour suivant, au lieu de
                # croire la commande passée.
                with self._lock:
                    self._last_pub[name] = 0.0
                if not self._warned_offline:
                    self._warned_offline = True
                    log.warning(
                        "Broker MQTT injoignable : la détection continue, "
                        "les commandes seront rejouées à la reconnexion."
                    )
            else:
                self._warned_offline = False
                log.info("%s <- %s", topic, json.dumps(payload))
