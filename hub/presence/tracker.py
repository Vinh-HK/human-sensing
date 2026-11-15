"""Suivi multi-personnes dans le repère appartement.

Chaîne de traitement, pour chaque trame d'un nœud radar :
  1. prédiction de toutes les pistes à l'instant de la trame (Kalman, vitesse constante) ;
  2. association détections <-> pistes (distance de Mahalanobis + algorithme hongrois) ;
  3. mise à jour des pistes associées ;
  4. détections libres : reprise d'une piste perdue à une porte (« handoff »)
     ou création d'une piste provisoire.

Gestion du cycle de vie (méthode step) :

  provisoire --(N détections)--> confirmée --(plus de détection)--+--> immobile (loin d'une porte)
                                     ^                            |
                                     |                            +--> à la porte --(délai)--> supprimée
                                     +---- nouvelle détection ----+        (ou immobile si le LD2410C
                                                                            voit encore quelqu'un)

Principe clé : **conservation des personnes**. Une personne ne disparaît pas
au milieu d'une pièce. Si le LD2450 la perd loin d'une porte, elle est
immobile (assise, couchée) : la piste reste, gelée sur place. Seul le capteur
de présence statique (LD2410C) peut libérer cette piste, s'il ne voit plus
personne dans la pièce pendant un temps long.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

from .config import EXTERIOR, Apartment
from .geometry import dist_point_segment

try:
    from scipy.optimize import linear_sum_assignment
except ImportError:  # repli glouton si scipy est absent
    linear_sum_assignment = None

TENTATIVE = "provisoire"
CONFIRMED = "confirmée"
HELD = "immobile"
AT_DOOR = "à_la_porte"
DELETED = "supprimée"

COUNTED = (CONFIRMED, HELD)

DEFAULTS = dict(
    meas_sigma_m=0.15, accel_sigma=3.0, gate_chi2=13.8,
    confirm_hits=4, confirm_window_s=1.0, tentative_timeout_s=0.6,
    miss_to_hold_s=1.5, hold_gate_m=1.2, door_margin_m=0.7, handoff_s=3.0,
    static_absent_s=45.0, hold_max_s=14400.0,
    coast_s=0.4, vmax=2.5, pos_sigma_max_m=0.5, merge_m=0.4, max_jump_m=1.5,
    wake_hits=3, wake_window_s=0.6, min_gate_m=0.7,
    no_birth_m=0.8, confirm_hits_far=8, confirm_window_far_s=2.0, warmup_s=20.0, room_hits=3,
)


@dataclass
class Track:
    id: int
    x: np.ndarray          # [x, y, vx, vy]
    P: np.ndarray
    t: float
    state: str = TENTATIVE
    hits: list[float] = field(default_factory=list)
    last_hit: float = 0.0
    room: str | None = None
    since: float = 0.0     # instant d'entrée dans l'état courant
    door: object = None    # porte associée quand state == AT_DOOR
    last_static_ok: float = 0.0
    trail: list = field(default_factory=list)  # (t, x, y) des dernières mesures
    wake: list = field(default_factory=list)   # détections récentes d'une piste immobile
    pending: list = field(default_factory=list)  # (t, pièce) : changement de pièce en attente

    @property
    def pos(self) -> tuple[float, float]:
        return (float(self.x[0]), float(self.x[1]))

    @property
    def speed(self) -> float:
        return float(math.hypot(self.x[2], self.x[3]))


@dataclass
class Event:
    t: float
    kind: str              # "entrée", "sortie", "naissance", "fin"
    track: int
    src: str | None
    dst: str | None
    note: str = ""

    def __str__(self) -> str:
        return f"[{self.t:8.1f}s] piste {self.track:3d} {self.kind:9s} {self.src or '-':>9s} -> {self.dst or '-':<9s} {self.note}"


class Tracker:
    def __init__(self, apt: Apartment, params: dict | None = None):
        self.apt = apt
        p = dict(DEFAULTS)
        p.update(params or {})
        self.p = p
        self.tracks: list[Track] = []
        self.events: list[Event] = []
        self._ids = itertools.count(1)
        r = p["meas_sigma_m"] ** 2
        self.R = np.diag([r, r])
        self.H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])

    # ---------------------------------------------------------------- Kalman

    def _predict(self, tr: Track, t: float) -> None:
        dt = t - tr.t
        if dt <= 0:
            return
        tr.t = t
        if tr.state in (HELD, AT_DOOR):
            return  # gelée : une personne immobile ne dérive pas
        if t - tr.last_hit > self.p["coast_s"]:
            # Plus de mesure depuis un moment : on n'extrapole plus. Un humain
            # qui disparaît du radar s'est en général arrêté, il n'a pas filé
            # en ligne droite à travers le mur.
            tr.x[2:] = 0.0
        prev = tr.x.copy()
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = self.p["accel_sigma"] ** 2
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        Q = G @ G.T * q
        tr.x = F @ tr.x
        tr.P = F @ tr.P @ F.T + Q
        # borne de l'incertitude : sinon la porte d'association grandit sans
        # limite et la piste finit par « avaler » un fantôme lointain
        vmax = self.p["pos_sigma_max_m"] ** 2
        for i in (0, 1):
            if tr.P[i, i] > vmax:
                k = math.sqrt(vmax / tr.P[i, i])
                tr.P[i, :] *= k
                tr.P[:, i] *= k   # P reste symétrique, P[i,i] = vmax
        # une personne ne sort pas de l'appartement par un mur
        if tr.room is not None and self.apt.nearest_room(tr.pos, 0.3) is None:
            tr.x = prev
            tr.x[2:] = 0.0

    def _update(self, tr: Track, z: np.ndarray) -> None:
        S = self.H @ tr.P @ self.H.T + self.R
        K = tr.P @ self.H.T @ np.linalg.inv(S)
        tr.x = tr.x + K @ (z - self.H @ tr.x)
        tr.P = (np.eye(4) - K @ self.H) @ tr.P
        v = math.hypot(tr.x[2], tr.x[3])
        if v > self.p["vmax"]:  # vitesse humaine plausible
            tr.x[2:] *= self.p["vmax"] / v

    def _reset_at(self, tr: Track, z: np.ndarray) -> None:
        tr.x = np.array([z[0], z[1], 0.0, 0.0])
        tr.P = np.diag([self.R[0, 0], self.R[1, 1], 1.0, 1.0])

    def _new_track(self, z: np.ndarray, t: float) -> Track:
        tr = Track(next(self._ids), np.zeros(4), np.eye(4), t, TENTATIVE, [t], t, since=t)
        self._reset_at(tr, z)
        tr.room = self.apt.nearest_room(tr.pos)
        tr.last_static_ok = t
        self.tracks.append(tr)
        return tr

    # ----------------------------------------------------------- association

    def _crossable(self, room_a: str | None, room_b: str | None, z: np.ndarray) -> bool:
        """Une piste ne change de pièce que par une porte, jamais à travers un mur."""
        if room_a is None or room_b is None or room_a == room_b:
            return True
        for d in self.apt.doors:
            if set(d.rooms) == {room_a, room_b} and \
                    dist_point_segment(tuple(z), d.a, d.b) < self.p["door_margin_m"]:
                return True
        return False

    def _cost(self, tr: Track, z: np.ndarray, det_room: str | None) -> float:
        if not self._crossable(tr.room, det_room, z):
            return math.inf
        if tr.state == HELD:
            if det_room is not None and tr.room != det_room:
                return math.inf
            d = math.hypot(z[0] - tr.x[0], z[1] - tr.x[1])
            g = self.p["hold_gate_m"]
            return (d / g) ** 2 * self.p["gate_chi2"] if d <= g else math.inf
        # garde-fou physique : distance parcourable depuis la dernière mesure
        jump = math.hypot(z[0] - tr.x[0], z[1] - tr.x[1])
        reach = min(self.p["max_jump_m"], 3 * self.p["meas_sigma_m"] + self.p["vmax"] * (tr.t - tr.last_hit))
        if jump > reach:
            return math.inf
        nu = z - self.H @ tr.x
        S = self.H @ tr.P @ self.H.T + self.R
        d2 = float(nu @ np.linalg.solve(S, nu))
        if d2 <= self.p["gate_chi2"]:
            return d2
        # Le modèle « vitesse constante » rate les virages serrés (on tourne
        # à 90° en sortant d'une porte). En dessous de min_gate_m, on accepte
        # quand même : à cette distance, c'est presque sûrement la même personne.
        if jump <= self.p["min_gate_m"]:
            return self.p["gate_chi2"]
        return math.inf

    def _assign(self, C: np.ndarray) -> list[tuple[int, int]]:
        if C.size == 0:
            return []
        if linear_sum_assignment is not None:
            big = 1e9
            Cf = np.where(np.isfinite(C), C, big)
            rows, cols = linear_sum_assignment(Cf)
            return [(r, c) for r, c in zip(rows, cols) if Cf[r, c] < big]
        pairs, used_r, used_c = [], set(), set()
        for idx in np.argsort(C, axis=None):
            r, c = np.unravel_index(idx, C.shape)
            if not np.isfinite(C[r, c]):
                break
            if r not in used_r and c not in used_c:
                pairs.append((r, c))
                used_r.add(r)
                used_c.add(c)
        return pairs

    def ingest(self, t: float, detections: list[tuple[float, float]], node_room: str) -> None:
        """Intègre les détections d'une trame (déjà en coordonnées monde)."""
        for tr in self.tracks:
            self._predict(tr, t)
        Z = [np.array(d, dtype=float) for d in detections]
        droom = [self.apt.nearest_room(tuple(z)) for z in Z]
        used = set()
        # Association en deux passes : les pistes établies choisissent d'abord,
        # les pistes provisoires se partagent les restes.
        pairs = []
        for group in ((CONFIRMED, HELD), (TENTATIVE,)):
            cands = [tr for tr in self.tracks if tr.state in group]
            free = [j for j in range(len(Z)) if j not in used]
            C = np.full((len(cands), len(free)), math.inf)
            for i, tr in enumerate(cands):
                for jj, j in enumerate(free):
                    C[i, jj] = self._cost(tr, Z[j], droom[j])
            for i, jj in self._assign(C):
                pairs.append((cands[i], free[jj]))
                used.add(free[jj])
        p_wake_s = self.p["wake_window_s"]
        for tr, j in pairs:
            z = Z[j]
            if tr.state == HELD:
                # Une détection isolée près d'une personne immobile la confirme
                # sans la « réveiller » : un fantôme ne doit pas déplacer la piste.
                tr.last_static_ok = t
                tr.wake = [w for w in tr.wake if t - w[0] <= p_wake_s] + [(t, z[0], z[1])]
                xs = [w[1] for w in tr.wake]
                ys = [w[2] for w in tr.wake]
                compact = (max(xs) - min(xs) < 0.6) and (max(ys) - min(ys) < 0.6)
                if len(tr.wake) >= self.p["wake_hits"] and compact:
                    tr.wake = []
                    # détections régulières : la personne se remet à bouger
                    self._reset_at(tr, z)
                    tr.last_hit = t
                    self._set_state(tr, CONFIRMED, t)
                    self._update_room(tr, t)
                else:
                    tr.x[:2] = 0.85 * tr.x[:2] + 0.15 * z
                continue
            self._update(tr, z)
            tr.hits.append(t)
            tr.last_hit = t
            tr.trail = [e for e in tr.trail if t - e[0] <= 3.0] + [(t, tr.x[0], tr.x[1])]
            self._update_room(tr, t)
        for j, z in enumerate(Z):
            if j in used:
                continue
            if self._handoff(z, t):
                continue
            # pas de naissance collée à une personne déjà suivie
            if any(tr.state == CONFIRMED and math.hypot(tr.x[0] - z[0], tr.x[1] - z[1]) < self.p["no_birth_m"]
                   for tr in self.tracks):
                continue
            self._new_track(z, t)

    def _handoff(self, z: np.ndarray, t: float) -> bool:
        """Une détection libre près d'une porte reprend une piste perdue à cette porte."""
        best, bd = None, 1.5 * self.p["door_margin_m"]
        for tr in self.tracks:
            if tr.state != AT_DOOR or t - tr.since > self.p["handoff_s"]:
                continue
            d = dist_point_segment(tuple(z), tr.door.a, tr.door.b)
            if d < bd:
                best, bd = tr, d
        if best is None:
            return False
        self._reset_at(best, z)
        best.hits.append(t)
        best.last_hit = t
        self._set_state(best, CONFIRMED, t)
        self._update_room(best, t)
        return True

    # --------------------------------------------------------------- pièces

    def _update_room(self, tr: Track, t: float) -> None:
        new = self.apt.room_of(tr.pos)
        if new is None or new == tr.room:
            tr.pending = []
            return
        if tr.room is not None and tr.state in COUNTED:
            # Hystérésis : il faut plusieurs mesures de l'autre côté de la
            # porte. Une mesure parasite ne fait pas changer de pièce.
            tr.pending = [e for e in tr.pending if t - e[0] <= 1.0 and e[1] == new] + [(t, new)]
            if len(tr.pending) < self.p["room_hits"]:
                return
        tr.pending = []
        old, tr.room = tr.room, new
        if tr.state in COUNTED:
            self.events.append(Event(t, "entrée", tr.id, old, new))

    def _set_state(self, tr: Track, state: str, t: float, note: str = "") -> None:
        if tr.state == state:
            return
        prev = tr.state
        tr.state, tr.since = state, t
        if state == CONFIRMED and prev == TENTATIVE:
            self.events.append(Event(t, "naissance", tr.id, None, tr.room, note))
        if state == DELETED and prev != TENTATIVE:
            self.events.append(Event(t, "fin", tr.id, tr.room, None, note))

    def _near_any_door(self, tr: Track) -> bool:
        return any(dist_point_segment(tr.pos, d.a, d.b) < self.p["door_margin_m"] for d in self.apt.doors)

    def _held_near(self, tr: Track):
        """Piste immobile que cette nouvelle piste doit reprendre (conservation).

        Personne n'apparaît au milieu d'une pièce. Une piste qui naît loin des
        portes est donc, en priorité, une personne immobile de la même pièce
        qui se remet à bouger — quelle que soit la distance. Près d'une porte,
        ce peut être un nouvel arrivant : on n'absorbe que dans hold_gate_m.
        """
        near_door = any(dist_point_segment(tr.pos, d.a, d.b) < self.p["door_margin_m"]
                        for d in self.apt.doors_of(tr.room)) if tr.room else True
        best, bd = None, (self.p["hold_gate_m"] if near_door else math.inf)
        for o in self.tracks:
            if o is tr or o.room != tr.room:
                continue
            lost = o.state == CONFIRMED and o.t - o.last_hit > self.p["coast_s"]
            if o.state != HELD and not lost:
                continue
            d = math.hypot(o.x[0] - tr.x[0], o.x[1] - tr.x[1])
            if d < bd:
                best, bd = o, d
        return best

    def _nearest_door(self, tr: Track):
        """Porte près de laquelle la piste a été vue récemment (pas seulement
        à sa dernière position : une mesure parasite peut l'avoir déplacée)."""
        if tr.room is None:
            return None
        pts = [(x, y) for (_, x, y) in tr.trail] or [tr.pos]
        best, bd = None, self.p["door_margin_m"]
        for d in self.apt.doors_of(tr.room):
            dist = min(dist_point_segment(q, d.a, d.b) for q in pts[-15:])
            if dist < bd:
                best, bd = d, dist
        return best

    # ------------------------------------------------------- cycle de vie

    def step(self, t: float, static_present) -> None:
        """static_present(room, t) -> (présent: bool|None, absent_depuis: float|None)."""
        p = self.p
        for tr in self.tracks:
            self._predict(tr, t)
            if tr.state == TENTATIVE:
                tr.hits = [h for h in tr.hits if t - h <= p["confirm_window_far_s"]]
                recent = [h for h in tr.hits if t - h <= p["confirm_window_s"]]
                if len(recent) >= p["confirm_hits"]:
                    held = self._held_near(tr)  # immobile ou perdue dans la même pièce
                    if held is not None:
                        # c'est la personne immobile qui se remet à bouger
                        held.x, held.P, held.last_hit = tr.x.copy(), tr.P.copy(), tr.last_hit
                        held.trail = []
                        self._set_state(held, CONFIRMED, t)
                        tr.state = DELETED
                    elif self._near_any_door(tr) or t < p["warmup_s"] \
                            or len(tr.hits) >= p["confirm_hits_far"]:
                        # Près d'une porte : un arrivant. Loin des portes : on
                        # exige plus de preuves (fantômes, multitrajets).
                        self._set_state(tr, CONFIRMED, t)
                elif t - tr.last_hit > p["tentative_timeout_s"]:
                    tr.state = DELETED
            elif tr.state == CONFIRMED:
                if t - tr.last_hit > p["miss_to_hold_s"]:
                    door = self._nearest_door(tr)
                    if door is not None:
                        tr.door = door
                        self._set_state(tr, AT_DOOR, t)
                    else:
                        tr.x[2:] = 0.0
                        tr.last_static_ok = t
                        self._set_state(tr, HELD, t)
            elif tr.state == AT_DOOR:
                if t - tr.since > p["handoff_s"]:
                    pres, _ = static_present(tr.room, t)
                    others = sum(1 for o in self.tracks
                                 if o is not tr and o.state in COUNTED and o.room == tr.room)
                    if pres and others == 0:
                        # personne arrêtée près de la porte, côté intérieur
                        tr.last_static_ok = t
                        self._set_state(tr, HELD, t, "arrêt près de la porte")
                    else:
                        dst = tr.door.other(tr.room)
                        note = "a quitté l'appartement" if dst == EXTERIOR else f"perdue vers {dst}"
                        self.events.append(Event(t, "sortie", tr.id, tr.room, dst, note))
                        tr.room = dst
                        self._set_state(tr, DELETED, t, note)
            elif tr.state == HELD:
                pres, absent_since = static_present(tr.room, t)
                if pres:
                    tr.last_static_ok = t
                elif pres is False and absent_since is not None and t - absent_since > p["static_absent_s"]:
                    self._set_state(tr, DELETED, t, "libérée : LD2410C ne voit plus personne")
                if tr.state == HELD and t - tr.last_static_ok > p["hold_max_s"]:
                    self._set_state(tr, DELETED, t, "garde-fou hold_max_s")
        self._merge_duplicates(t)
        self.tracks = [tr for tr in self.tracks if tr.state != DELETED]

    def _merge_duplicates(self, t: float) -> None:
        """Deux pistes comptées au même endroit = une seule personne.

        Cas typique : une personne immobile (piste gelée) bouge un peu, le
        radar la revoit à 50 cm de sa piste gelée et crée une nouvelle piste.
        On garde l'identifiant le plus ancien, à la position la plus récente.
        """
        live = [tr for tr in self.tracks if tr.state in COUNTED]
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                if a.state == DELETED or b.state == DELETED or a.room != b.room:
                    continue
                d = math.hypot(a.x[0] - b.x[0], a.x[1] - b.x[1])
                lim = self.p["merge_m"] if HELD not in (a.state, b.state) else self.p["hold_gate_m"]
                if d > lim:
                    continue
                old, new = (a, b) if a.id < b.id else (b, a)
                if new.state == HELD and old.state == CONFIRMED:
                    new.state = DELETED
                    continue
                old.x, old.P, old.last_hit = new.x.copy(), new.P.copy(), max(old.last_hit, new.last_hit)
                old.state, old.since = new.state, t
                new.state = DELETED  # fusion silencieuse (pas d'événement « fin »)

    def occupancy(self) -> dict[str, int]:
        occ = {name: 0 for name in self.apt.rooms}
        for tr in self.tracks:
            if tr.state in COUNTED and tr.room in occ:
                occ[tr.room] += 1
        return occ

    def pop_events(self) -> list[Event]:
        ev, self.events = self.events, []
        return ev
