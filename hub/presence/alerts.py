"""Alertes de santé du système.

Une alerte est identifiée par une clé (ex. "proche:n-sdb"). Elle est levée une
seule fois tant que la condition dure, puis close quand la condition cesse.
Chaque changement est transmis aux « sorties » : MQTT, notification téléphone
(ntfy), tableau de bord web.

Alertes produites par le moteur (voir engine.py) :
  proche      cibles répétées à moins de min_range_m du LD2450
              -> insecte posé, araignée, toile, objet contre le boîtier
  statique    LD2410C : présence immobile à moins de static_near_cm
              -> même causes, vues par le radar de présence
  muet        plus aucune trame d'un nœud -> alimentation, Wi-Fi, câble
  fantome     LD2410C voit quelqu'un longtemps, mais aucune personne suivie
              -> animal, ventilateur, rideau, ou personne entrée sans être vue
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

LEVELS = ("info", "attention", "erreur")


@dataclass
class Alert:
    key: str
    level: str
    title: str
    detail: str
    since: float
    node: str | None = None
    room: str | None = None
    active: bool = True
    ended: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class AlertManager:
    def __init__(self, history: int = 50):
        self.active: dict[str, Alert] = {}
        self.history: list[Alert] = []
        self.max_history = history
        self.sinks = []          # fonctions appelées à chaque changement : f(alert)
        self.lock = threading.Lock()

    def raise_(self, key: str, level: str, title: str, detail: str, t: float,
               node: str | None = None, room: str | None = None) -> None:
        with self.lock:
            if key in self.active:
                return
            a = Alert(key, level, title, detail, t, node, room)
            self.active[key] = a
            self.history.insert(0, a)
            del self.history[self.max_history:]
        log.warning("ALERTE %s : %s — %s", level, title, detail)
        self._emit(a)

    def clear(self, key: str, t: float) -> None:
        with self.lock:
            a = self.active.pop(key, None)
            if a is None:
                return
            a.active, a.ended = False, t
        log.info("alerte close : %s", a.title)
        self._emit(a)

    def is_active(self, key: str) -> bool:
        return key in self.active

    def snapshot(self) -> dict:
        with self.lock:
            return {"active": [a.to_dict() for a in self.active.values()],
                    "history": [a.to_dict() for a in self.history[:20]]}

    def _emit(self, a: Alert) -> None:
        for sink in self.sinks:
            try:
                sink(a)
            except Exception as e:  # une sortie en panne ne doit pas casser le suivi
                log.error("sortie d'alerte en erreur : %s", e)


def mqtt_sink(client, topic: str):
    def sink(a: Alert):
        client.publish(topic, json.dumps(a.to_dict(), ensure_ascii=False), qos=1)
    return sink


def ntfy_sink(url: str, min_level: str = "attention"):
    """Notification téléphone via ntfy (application gratuite, Android et iOS).

    url : https://ntfy.sh/<sujet-difficile-à-deviner> ou votre propre serveur ntfy.
    Envoi dans un fil séparé : une coupure Internet ne bloque pas le hub.
    """
    rank = {lvl: i for i, lvl in enumerate(LEVELS)}

    base, _, topic = url.rstrip("/").rpartition("/")

    def post(a: Alert):
        # Publication JSON : les accents passent sans encodage d'en-tête HTTP.
        prefix = "Résolu : " if not a.active else ""
        body = {"topic": topic, "title": prefix + a.title, "message": a.detail,
                "priority": 4 if a.level == "erreur" else 3,
                "tags": ["warning"] if a.active else ["white_check_mark"]}
        req = urllib.request.Request(base, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            log.error("ntfy injoignable : %s", e)

    def sink(a: Alert):
        if rank.get(a.level, 0) >= rank[min_level]:
            threading.Thread(target=post, args=(a,), daemon=True).start()
    return sink
