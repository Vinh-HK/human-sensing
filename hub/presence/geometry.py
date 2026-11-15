"""Géométrie 2D : polygones des pièces, segments des portes, repères capteur.

Convention du repère capteur (identique au LD2450) :
  - y_s : axe de visée du radar (toujours > 0 devant le capteur) ;
  - x_s : à droite du radar quand on regarde dans l'axe de visée.

Convention du repère appartement (monde) :
  - x vers l'est, y vers le nord, unités en mètres ;
  - la pose d'un capteur est (px, py, theta) où theta est la direction
    de l'axe de visée, mesurée depuis l'axe x monde, sens trigonométrique.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

Point = tuple[float, float]


def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    """Test du rayon (ray casting). Un point sur un bord est indéterminé."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi > x:
                inside = not inside
    return inside


def dist_point_segment(p: Point, a: Point, b: Point) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0.0:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


def dist_point_polygon(p: Point, poly: Sequence[Point]) -> float:
    """Distance au bord du polygone (0 si à l'intérieur)."""
    if point_in_polygon(p, poly):
        return 0.0
    n = len(poly)
    return min(dist_point_segment(p, poly[i], poly[(i + 1) % n]) for i in range(n))


def sensor_to_world(xs: float, ys: float, pose: tuple[float, float, float]) -> Point:
    px, py, th = pose
    c, s = math.cos(th), math.sin(th)
    return (px + ys * c + xs * s, py + ys * s - xs * c)


def world_to_sensor(wx: float, wy: float, pose: tuple[float, float, float]) -> Point:
    px, py, th = pose
    c, s = math.cos(th), math.sin(th)
    dx, dy = wx - px, wy - py
    return (dx * s - dy * c, dx * c + dy * s)


def in_fov(wx: float, wy: float, pose, max_range: float, half_fov_rad: float) -> bool:
    xs, ys = world_to_sensor(wx, wy, pose)
    if ys <= 0.0:
        return False
    if math.hypot(xs, ys) > max_range:
        return False
    return abs(math.atan2(xs, ys)) <= half_fov_rad


def fit_pose(sensor_pts: Iterable[Point], world_pts: Iterable[Point]) -> tuple[float, float, float, float]:
    """Estime la pose d'un capteur par moindres carrés (Kabsch 2D).

    On marche sur des points connus (repères au sol), on enregistre ce que
    le radar mesure, et on cherche la transformation rigide qui envoie les
    points capteur sur les points monde.

    Retourne (px, py, theta, rmse_m).
    """
    A = np.asarray(list(sensor_pts), dtype=float)
    B = np.asarray(list(world_pts), dtype=float)
    if A.shape != B.shape or len(A) < 2:
        raise ValueError("il faut au moins 2 paires de points de même taille")
    a0, b0 = A.mean(axis=0), B.mean(axis=0)
    Ac, Bc = A - a0, B - b0
    # Rotation R(phi) qui minimise sum |R a - b|^2
    num = np.sum(Ac[:, 0] * Bc[:, 1] - Ac[:, 1] * Bc[:, 0])
    den = np.sum(Ac[:, 0] * Bc[:, 0] + Ac[:, 1] * Bc[:, 1])
    phi = math.atan2(num, den)
    R = np.array([[math.cos(phi), -math.sin(phi)], [math.sin(phi), math.cos(phi)]])
    t = b0 - R @ a0
    # Le passage capteur -> monde est une rotation d'angle theta - 90°.
    theta = phi + math.pi / 2
    resid = (A @ R.T + t) - B
    rmse = float(np.sqrt(np.mean(np.sum(resid**2, axis=1))))
    return float(t[0]), float(t[1]), (theta + math.pi) % (2 * math.pi) - math.pi, rmse
