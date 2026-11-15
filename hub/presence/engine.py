"""Moteur : trames des nœuds -> suivi -> occupation des pièces -> commandes de lumière."""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

from .config import Apartment
from .geometry import dist_point_polygon, point_in_polygon, sensor_to_world
from .alerts import AlertManager
from .tracker import COUNTED, Event, Tracker

ALERT_DEFAULTS = dict(
    min_range_m=0.5,        # cible plus proche que ça du LD2450 : rejetée (insecte, objet)
    near_window_s=30.0,
    near_hits=40,           # nb de cibles trop proches dans la fenêtre -> alerte
    static_near_cm=50,      # LD2410C : présence immobile plus proche que ça…
    static_near_s=120.0,    # …pendant ce temps -> alerte
    node_silent_s=15.0,     # plus de trame depuis ce temps -> nœud muet
    phantom_static_s=600.0, # LD2410C voit quelqu'un, aucune piste, pendant ce temps
)

log = logging.getLogger(__name__)


@dataclass
class StaticState:
    present: bool = False
    t: float = -math.inf          # instant du dernier rapport
    absent_since: float | None = None


class LightLogic:
    """Allumage immédiat à l'entrée, extinction après off_delay_s de pièce vide."""

    def __init__(self, apt: Apartment, off_delay_s: float = 5.0):
        self.apt = apt
        self.off_delay = off_delay_s
        self.on = {r: False for r in apt.rooms}
        self.empty_since: dict[str, float | None] = {r: None for r in apt.rooms}

    def update(self, t: float, occ: dict[str, int], events: list[Event]) -> list[tuple[str, bool]]:
        cmds = []
        entered = {e.dst for e in events if e.kind in ("entrée", "naissance")}
        for room in self.apt.rooms:
            if occ[room] > 0 or room in entered:
                self.empty_since[room] = None
                if not self.on[room]:
                    self.on[room] = True
                    cmds.append((room, True))
            else:
                if self.empty_since[room] is None:
                    self.empty_since[room] = t
                if self.on[room] and t - self.empty_since[room] >= self.off_delay:
                    self.on[room] = False
                    cmds.append((room, False))
        return cmds


class Engine:
    def __init__(self, apt: Apartment, lights=None):
        self.apt = apt
        self.tracker = Tracker(apt, apt.params)
        z = apt.raw.get("zigbee", {})
        self.logic = LightLogic(apt, float(z.get("off_delay_s", 5.0)))
        s = apt.raw.get("sensor", {})
        self.max_range = float(s.get("max_range_m", 6.0))
        self.half_fov = math.radians(float(s.get("half_fov_deg", 60)))
        self.static_valid = float(s.get("static_valid_s", 2.0))
        self.static = {nid: StaticState() for nid in apt.nodes}
        self.lights = lights
        self.event_log: list[Event] = []
        self.rejected = 0
        a = dict(ALERT_DEFAULTS)
        a.update({k: v for k, v in apt.raw.get("alerts", {}).items() if k in ALERT_DEFAULTS})
        self.ap = a
        self.alerts = AlertManager()
        self.near = {nid: deque() for nid in apt.nodes}
        self.last_frame: dict[str, float | None] = {nid: None for nid in apt.nodes}
        self.static_near_since: dict[str, float | None] = {nid: None for nid in apt.nodes}
        self.phantom_since: dict[str, float | None] = {r: None for r in apt.rooms}
        self.t_start: float | None = None

    # ------------------------------------------------------------ entrées

    def ingest_frame(self, node_id: str, t: float, targets_mm, presence: dict | None = None) -> None:
        """targets_mm : liste de (x_mm, y_mm[, vitesse]) dans le repère du capteur."""
        node = self.apt.nodes.get(node_id)
        if node is None:
            log.warning("nœud inconnu %s", node_id)
            return
        room = self.apt.rooms[node.room]
        self.last_frame[node_id] = t
        dets = []
        for tgt in targets_mm:
            if math.hypot(tgt[0], tgt[1]) / 1000.0 < self.ap["min_range_m"]:
                # Un humain n'est presque jamais collé à un capteur posé en
                # hauteur : insecte, araignée, objet. On ignore et on compte.
                self.near[node_id].append(t)
                self.rejected += 1
                continue
            wx, wy = sensor_to_world(tgt[0] / 1000.0, tgt[1] / 1000.0, node.pose)
            # Le 24 GHz traverse en partie les cloisons : on rejette ce qui sort
            # de la pièce du nœud (avec une marge pour l'embrasure des portes).
            if dist_point_polygon((wx, wy), room.polygon) > 0.3:
                self.rejected += 1
                continue
            if any(point_in_polygon((wx, wy), ex) for ex in node.exclude):
                self.rejected += 1
                continue
            dets.append((wx, wy))
        self.tracker.ingest(t, dets, node.room)
        if presence is not None:
            self._ingest_static(node_id, t, bool(presence.get("state", 0)))
            sta = presence.get("sta")
            if (presence.get("state", 0) & 2) and sta is not None and 0 < sta < self.ap["static_near_cm"]:
                if self.static_near_since[node_id] is None:
                    self.static_near_since[node_id] = t
            else:
                self.static_near_since[node_id] = None

    def _ingest_static(self, node_id: str, t: float, present: bool) -> None:
        st = self.static[node_id]
        if present:
            st.absent_since = None
        elif st.present or st.absent_since is None:
            st.absent_since = t
        st.present, st.t = present, t

    def static_present(self, room: str, t: float):
        """(présent, absent_depuis) agrégé sur les nœuds de la pièce. None si pas de LD2410C."""
        states = [self.static[n.id] for n in self.apt.nodes.values()
                  if n.room == room and t - self.static[n.id].t <= self.static_valid]
        if not states:
            return None, None
        if any(s.present for s in states):
            return True, None
        since = max((s.absent_since for s in states if s.absent_since is not None), default=t)
        return False, since

    # ------------------------------------------------------------ cycle

    def step(self, t: float) -> list[tuple[str, bool]]:
        if self.t_start is None:
            self.t_start = t
        self.tracker.step(t, self.static_present)
        events = self.tracker.pop_events()
        for e in events:
            log.info("%s", e)
        self.event_log.extend(events)
        occ = self.tracker.occupancy()
        self._health(t, occ)
        cmds = self.logic.update(t, occ, events)
        if self.lights is not None:
            for room, on in cmds:
                for lamp in self.apt.rooms[room].lights:
                    self.lights.set(lamp, on)
        return cmds

    # ------------------------------------------------------------ santé

    def _health(self, t: float, occ: dict[str, int]) -> None:
        ap, A = self.ap, self.alerts
        for nid, node in self.apt.nodes.items():
            dq = self.near[nid]
            while dq and t - dq[0] > ap["near_window_s"]:
                dq.popleft()
            key = f"proche:{nid}"
            if len(dq) >= ap["near_hits"]:
                A.raise_(key, "attention", f"Objet collé au capteur {nid}",
                         f"Cibles répétées à moins de {ap['min_range_m']*100:.0f} cm du radar de suivi "
                         f"({node.room}). Vérifier : insecte posé, araignée ou toile, objet contre le boîtier. "
                         "Ces cibles sont ignorées.", t, nid, node.room)
            elif len(dq) <= ap["near_hits"] // 4:
                A.clear(key, t)

            since = self.static_near_since[nid]
            key = f"statique:{nid}"
            if since is not None and t - since > ap["static_near_s"]:
                A.raise_(key, "attention", f"Présence collée au capteur {nid}",
                         f"Le LD2410C voit une présence immobile à moins de {ap['static_near_cm']} cm "
                         f"depuis plus de {ap['static_near_s']:.0f} s ({node.room}). Une personne immobile "
                         "risque d'être gardée à tort : la lumière peut rester allumée.", t, nid, node.room)
            elif since is None:
                A.clear(key, t)

            last = self.last_frame[nid]
            ref = last if last is not None else self.t_start
            key = f"muet:{nid}"
            if t - ref > ap["node_silent_s"]:
                A.raise_(key, "erreur", f"Nœud {nid} muet",
                         f"Aucune trame depuis plus de {ap['node_silent_s']:.0f} s ({node.room}). "
                         "Vérifier : alimentation, Wi-Fi, câblage du radar.", t, nid, node.room)
            else:
                A.clear(key, t)

        for room in self.apt.rooms:
            pres, _ = self.static_present(room, t)
            key = f"fantome:{room}"
            if pres and occ[room] == 0:
                if self.phantom_since[room] is None:
                    self.phantom_since[room] = t
                if t - self.phantom_since[room] > ap["phantom_static_s"]:
                    A.raise_(key, "info", f"Présence sans personne suivie : {room}",
                             f"Le LD2410C voit quelqu'un depuis {ap['phantom_static_s']/60:.0f} min mais aucune "
                             "personne n'est suivie. Causes possibles : animal, ventilateur, rideau, "
                             "ou personne entrée sans être vue.", t, None, room)
            else:
                self.phantom_since[room] = None
                A.clear(key, t)

    def snapshot(self) -> dict:
        return {
            "occupancy": self.tracker.occupancy(),
            "lights": dict(self.logic.on),
            "tracks": [
                {"id": tr.id, "x": round(tr.pos[0], 2), "y": round(tr.pos[1], 2),
                 "state": tr.state, "room": tr.room}
                for tr in self.tracker.tracks if tr.state in COUNTED
            ],
        }
