"""Chargement et validation de la configuration de l'appartement."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .geometry import Point, dist_point_polygon, point_in_polygon

EXTERIOR = "exterieur"


@dataclass
class Room:
    name: str
    polygon: list[Point]
    lights: list[str] = field(default_factory=list)


@dataclass
class Door:
    name: str
    a: Point
    b: Point
    rooms: tuple[str, str]

    @property
    def center(self) -> Point:
        return ((self.a[0] + self.b[0]) / 2, (self.a[1] + self.b[1]) / 2)

    def other(self, room: str) -> str:
        return self.rooms[1] if self.rooms[0] == room else self.rooms[0]


@dataclass
class Node:
    id: str
    room: str
    pose: tuple[float, float, float]  # x, y, theta (rad)
    exclude: list[list[Point]] = field(default_factory=list)


@dataclass
class Apartment:
    rooms: dict[str, Room]
    doors: list[Door]
    nodes: dict[str, Node]
    raw: dict

    def room_of(self, p: Point) -> str | None:
        for r in self.rooms.values():
            if point_in_polygon(p, r.polygon):
                return r.name
        return None

    def nearest_room(self, p: Point, max_dist: float = 0.4) -> str | None:
        """Pièce contenant p, sinon la plus proche à moins de max_dist (piste sur un mur)."""
        r = self.room_of(p)
        if r is not None:
            return r
        best, bd = None, max_dist
        for room in self.rooms.values():
            d = dist_point_polygon(p, room.polygon)
            if d < bd:
                best, bd = room.name, d
        return best

    def doors_of(self, room: str) -> list[Door]:
        return [d for d in self.doors if room in d.rooms]

    @property
    def params(self) -> dict:
        return self.raw.get("tracker", {})


def load(path: str | Path) -> Apartment:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return from_dict(raw)


def from_dict(raw: dict) -> Apartment:
    rooms = {}
    for r in raw["rooms"]:
        if r["name"] == EXTERIOR:
            raise ValueError(f"'{EXTERIOR}' est un nom réservé")
        rooms[r["name"]] = Room(r["name"], [tuple(p) for p in r["polygon"]], list(r.get("lights", [])))
    doors = []
    for d in raw.get("doors", []):
        rs = tuple(d["rooms"])
        for name in rs:
            if name != EXTERIOR and name not in rooms:
                raise ValueError(f"porte {d['name']} : pièce inconnue {name}")
        doors.append(Door(d["name"], tuple(d["a"]), tuple(d["b"]), rs))
    nodes = {}
    for n in raw.get("nodes", []):
        if n["room"] not in rooms:
            raise ValueError(f"nœud {n['id']} : pièce inconnue {n['room']}")
        x, y, th = n["pose"]
        nodes[n["id"]] = Node(n["id"], n["room"], (float(x), float(y), math.radians(th)),
                              [[tuple(p) for p in poly] for poly in n.get("exclude", [])])
    return Apartment(rooms, doors, nodes, raw)
