"""Les trois détecteurs, un par typologie de pièce.

Contrat commun : chaque détecteur consomme un bloc de série temps-lent et rend un
score en dB. La décision est toujours `score > baseline + threshold_db`, où
`baseline` vient du calibrage (mesure pièce vide) et `threshold_db` de la config.
Ça rend les trois algorithmes interchangeables et calibrables de la même façon.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import signal

from .dsp import EPS, Decimator, RingBuffer, StreamingSOS


@dataclass
class Detection:
    score_db: float
    detected: bool
    info: dict = field(default_factory=dict)


def _pow2_at_most(n: int, cap: int) -> int:
    """Plus grande puissance de 2 <= min(n, cap), au moins 32."""
    v = 32
    while v * 2 <= min(n, cap):
        v *= 2
    return v


class Detector:
    """Classe de base. `STREAM` indique quel flux de la source consommer."""

    STREAM = "mean"
    name = "base"

    def __init__(self, fs: float, cfg: dict, baseline_db: float = 0.0):
        self.fs = float(fs)
        self.cfg = cfg
        self.baseline_db = float(baseline_db)
        self.threshold_db = float(cfg.get("threshold_db", 6.0))

    def _decide(self, score_db: float) -> bool:
        return score_db > (self.baseline_db + self.threshold_db)

    def update(self, x: np.ndarray, presence_hint: bool = False) -> Optional[Detection]:
        raise NotImplementedError


class ShowerDetector(Detector):
    """Salle de bain — variance de l'enveloppe dans la bande « mouvement ».

    L'eau en mouvement et un corps proche de l'antenne produisent des
    fluctuations larges et fortes du canal. Un simple niveau de variance suffit,
    à condition de retirer d'abord la dérive lente (thermique, AGC, changement de
    débit de l'AP) avec un passe-bande — sinon la variance mesure la dérive.

    Note honnête : ce détecteur ne distingue pas « douche qui coule avec
    quelqu'un » de « douche qui coule à vide ». En pratique c'est sans
    conséquence : si l'eau coule, il y a quelqu'un.
    """

    name = "shower"
    STREAM = "mean"

    def __init__(self, fs, cfg, baseline_db=0.0):
        super().__init__(fs, cfg, baseline_db)
        lo = float(cfg.get("band_lo_hz", 0.3))
        hi = min(float(cfg.get("band_hi_hz", 20.0)), 0.45 * fs)
        self.band = StreamingSOS(
            signal.butter(2, [lo, hi], "bandpass", fs=fs, output="sos")
        )
        self.ring = RingBuffer(int(float(cfg.get("window_s", 4.0)) * fs))

    def update(self, x, presence_hint=False):
        y = self.band(x)
        self.ring.push(y)
        if not self.ring.ready:
            return Detection(-np.inf, False, {"status": "warmup"})
        var = float(np.var(self.ring.data()))
        score = 10.0 * np.log10(var + EPS)
        return Detection(score, self._decide(score), {"var": var})


class BreathingDetector(Detector):
    """Salon — raie spectrale de la respiration (0,13–0,60 Hz ≈ 8–36 cycles/min).

    Un humain parfaitement immobile module quand même le canal : la cage
    thoracique se déplace de quelques millimètres, ce qui suffit à faire varier
    la longueur des trajets multiples. L'amplitude de cet effet est minuscule, il
    faut donc une longue observation cohérente (fenêtre de 60 s) pour que la raie
    émerge : la résolution fréquentielle vaut 1/T, il est impossible de résoudre
    0,25 Hz sur un bloc court.

    Deux garde-fous contre les faux positifs (ventilateur, pendule, chargeur à
    découpage) : la raie doit dominer un plancher de référence mesuré au-dessus
    de la bande respiratoire, et sa fréquence doit rester stable sur plusieurs
    fenêtres consécutives. Un signal parasite mécanique est en général *trop*
    stable et hors bande ; un artefact aléatoire, lui, saute d'une fenêtre à
    l'autre et se fait éliminer par le test de stabilité.
    """

    name = "breathing"
    STREAM = "primary"

    def __init__(self, fs, cfg, baseline_db=0.0):
        super().__init__(fs, cfg, baseline_db)
        target = float(cfg.get("work_rate_hz", 10.0))
        self.dec = Decimator(max(1, int(round(fs / target))), fs)
        self.fs2 = fs / self.dec.factor

        self.lo = float(cfg.get("band_lo_hz", 0.13))
        self.hi = min(float(cfg.get("band_hi_hz", 0.60)), 0.4 * self.fs2)
        self.band = StreamingSOS(
            signal.butter(
                2,
                [max(0.05, self.lo * 0.7), min(self.hi * 1.4, 0.45 * self.fs2)],
                "bandpass",
                fs=self.fs2,
                output="sos",
            )
        )
        self.window_s = float(cfg.get("window_s", 60.0))
        self.ring = RingBuffer(int(self.window_s * self.fs2))
        self.tol_hz = float(cfg.get("stability_tol_hz", 0.06))
        self.n_stable = int(cfg.get("stability_windows", 3))
        self.peaks = deque(maxlen=self.n_stable)

    def update(self, x, presence_hint=False):
        xd = self.dec(x)
        if xd.size == 0:
            return None
        y = self.band(xd)
        self.ring.push(y)
        if not self.ring.ready:
            return Detection(-np.inf, False, {"status": "warmup"})

        d = self.ring.data()
        nperseg = _pow2_at_most(d.size, int(round(25.6 * self.fs2)))
        f, P = signal.welch(
            d, fs=self.fs2, nperseg=nperseg, noverlap=nperseg // 2, detrend="linear"
        )

        in_band = (f >= self.lo) & (f <= self.hi)
        ref_hi = min(3.0, 0.45 * self.fs2)
        ref = (f >= 1.0) & (f <= ref_hi)
        if not in_band.any() or not ref.any():
            return Detection(-np.inf, False, {"status": "band_unavailable"})

        k = int(np.argmax(P[in_band]))
        peak = float(P[in_band][k])
        fpk = float(f[in_band][k])
        floor = float(np.median(P[ref]))
        score = 10.0 * np.log10((peak + EPS) / (floor + EPS))

        self.peaks.append(fpk)
        stable = len(self.peaks) == self.peaks.maxlen and (
            max(self.peaks) - min(self.peaks) <= self.tol_hz
        )

        return Detection(
            score,
            self._decide(score) and stable,
            {"bpm": fpk * 60.0, "stable": stable, "f_peak_hz": fpk},
        )


class KitchenDetector(Detector):
    """Cuisine / buanderie — résidu spectral après soustraction du fond appris.

    Le découpage fréquentiel fixe proposé initialement (« ignorer > 300 Hz »)
    ne tient pas : à 2,4 GHz le décalage Doppler vaut ~16 Hz par m/s, donc un
    tambour en essorage sort effectivement vers 500 Hz, mais le même tambour en
    cycle de lavage tourne à ~50 tr/min et retombe vers 20 Hz — exactement là où
    se trouve l'activité humaine. Aucune coupure fixe ne sépare les deux.

    À la place on exploite la vraie différence : une machine est *stationnaire*
    (spectre stable pendant des minutes), un humain est *transitoire*. On garde
    donc une médiane glissante du spectre comme référence de fond, et on ne
    déclenche que sur ce qui dépasse cette référence dans la bande humaine.
    La médiane (et non la moyenne) rend la référence insensible aux bouffées
    courtes, qui sont précisément ce qu'on veut détecter.

    L'adaptation est **asymétrique**, et c'est le point crucial. Un fond qui
    suivrait le spectre de manière symétrique finirait par absorber la personne
    qu'on cherche : si le démon démarre alors que quelqu'un est déjà dans la
    pièce, cette personne devient le fond, n'est donc jamais détectée, donc
    `presence_hint` reste faux, donc le fond continue de l'absorber — l'échec
    s'auto-entretient. On impose donc :

      * **descente rapide** (``bg_fall_s``, ~20 s) : dès que la pièce redevient
        calme, la référence retombe et le détecteur est de nouveau armé ;
      * **montée lente** (``bg_rise_s``, ~10 min) : une augmentation d'énergie
        n'est jamais absorbée assez vite pour masquer une présence ;
      * **montée gelée** tant qu'une présence est affirmée (`presence_hint`),
        la descente restant autorisée pour se réarmer au plus tôt.

    Contrepartie assumée : quand une machine démarre, son spectre met
    ``bg_rise_s`` à être absorbé, donc la lumière peut rester allumée une dizaine
    de minutes de trop, une fois, au lancement du lave-vaisselle. C'est le bon
    sens du compromis — l'inverse (rater un humain) est bien plus gênant.
    """

    name = "kitchen"
    STREAM = "mean"

    def __init__(self, fs, cfg, baseline_db=0.0):
        super().__init__(fs, cfg, baseline_db)
        self.window_s = float(cfg.get("window_s", 10.0))
        self.ring = RingBuffer(int(self.window_s * fs))
        self.human = (
            float(cfg.get("human_lo_hz", 0.5)),
            min(float(cfg.get("human_hi_hz", 12.0)), 0.45 * fs),
        )
        self.machine = (
            float(cfg.get("machine_lo_hz", 15.0)),
            min(float(cfg.get("machine_hi_hz", 50.0)), 0.45 * fs),
        )
        self.margin_lin = 10.0 ** (float(cfg.get("bg_margin_db", 3.0)) / 10.0)
        self.fall_s = float(cfg.get("bg_fall_s", 20.0))
        self.rise_s = float(cfg.get("bg_rise_s", 600.0))
        self.min_bg_s = float(cfg.get("bg_min_s", 30.0))
        self.floor_db = float(cfg.get("score_floor_db", -60.0))
        self.bg: np.ndarray | None = None
        self._learned_s = 0.0
        self._nperseg = None

    def update(self, x, presence_hint=False):
        dt = len(x) / self.fs
        self.ring.push(x)
        if not self.ring.ready:
            return Detection(-np.inf, False, {"status": "warmup"})

        d = self.ring.data()
        if self._nperseg is None:
            self._nperseg = _pow2_at_most(d.size, int(round(2.5 * self.fs)))
        f, P = signal.welch(
            d, fs=self.fs, nperseg=self._nperseg,
            noverlap=self._nperseg // 2, detrend="linear",
        )

        if self.bg is None:
            self.bg = P.copy()
        else:
            a_fall = 1.0 - np.exp(-dt / max(self.fall_s, 1e-3))
            a_rise = 1.0 - np.exp(-dt / max(self.rise_s, 1e-3))
            alpha = np.where(P < self.bg, a_fall, 0.0 if presence_hint else a_rise)
            self.bg += alpha * (P - self.bg)

        self._learned_s += dt
        if self._learned_s < self.min_bg_s:
            return Detection(
                -np.inf, False,
                {"status": "learning_bg", "learned_s": round(self._learned_s, 1)},
            )

        resid = np.maximum(P - self.bg * self.margin_lin, 0.0)
        hm = (f >= self.human[0]) & (f <= self.human[1])
        score = max(
            self.floor_db,
            10.0 * np.log10((resid[hm].sum() + EPS) / (self.bg[hm].sum() + EPS)),
        )

        info: dict = {}
        mm = (f >= self.machine[0]) & (f <= self.machine[1])
        if mm.any():
            # Diagnostic seulement : utile pour vérifier qu'une machine tourne,
            # jamais utilisé pour décider (cf. docstring).
            info["machine_ratio_db"] = float(
                10.0 * np.log10((P[mm].sum() + EPS) / (P[hm].sum() + EPS))
            )
        return Detection(score, self._decide(score), info)


DETECTORS = {
    "shower": ShowerDetector,
    "breathing": BreathingDetector,
    "kitchen": KitchenDetector,
}


def build_detector(kind: str, fs: float, cfg: dict, baseline_db: float = 0.0) -> Detector:
    if kind not in DETECTORS:
        raise ValueError(
            f"Type de détecteur inconnu : {kind!r} (attendu : {sorted(DETECTORS)})"
        )
    return DETECTORS[kind](fs, cfg, baseline_db)
