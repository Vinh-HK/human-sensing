"""Moteur : trames des nœuds -> suivi -> occupation des pièces -> commandes de lumière."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .config import Apartment
from .geometry import dist_point_polygon, point_in_polygon, sensor_to_world
from .tracker import COUNTED, Event, Tracker

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

    # ------------------------------------------------------------ entrées

    def ingest_frame(self, node_id: str, t: float, targets_mm, presence: dict | None = None) -> None:
        """targets_mm : liste de (x_mm, y_mm[, vitesse]) dans le repère du capteur."""
        node = self.apt.nodes.get(node_id)
        if node is None:
            log.warning("nœud inconnu %s", node_id)
            return
        room = self.apt.rooms[node.room]
        dets = []
        for tgt in targets_mm:
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
        self.tracker.step(t, self.static_present)
        events = self.tracker.pop_events()
        for e in events:
            log.info("%s", e)
        self.event_log.extend(events)
        occ = self.tracker.occupancy()
        cmds = self.logic.update(t, occ, events)
        if self.lights is not None:
            for room, on in cmds:
                for lamp in self.apt.rooms[room].lights:
                    self.lights.set(lamp, on)
        return cmds

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
