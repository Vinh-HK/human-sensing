"""Briques de traitement du signal communes à toutes les sources.

Tout le système travaille sur des séries « temps lent » (slow-time) : une valeur
scalaire par tick (typiquement 20 à 100 Hz) représentant le gain du canal radio.
C'est cette série, et pas les échantillons I/Q bruts, qui porte l'information de
présence : le corps humain modifie les trajets multiples, donc l'amplitude reçue.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

EPS = 1e-20


class RingBuffer:
    """Tampon glissant de taille fixe (le plus ancien sort par la gauche)."""

    def __init__(self, size: int, dtype=np.float32):
        self.size = max(1, int(size))
        self.buf = np.zeros(self.size, dtype=dtype)
        self.filled = 0

    def push(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=self.buf.dtype).ravel()
        n = x.size
        if n == 0:
            return
        if n >= self.size:
            self.buf[:] = x[-self.size :]
            self.filled = self.size
            return
        self.buf[:-n] = self.buf[n:]
        self.buf[-n:] = x
        self.filled = min(self.size, self.filled + n)

    @property
    def ready(self) -> bool:
        return self.filled >= self.size

    def data(self) -> np.ndarray:
        return self.buf


class StreamingSOS:
    """Filtre SOS à état persistant.

    On filtre bloc par bloc en conservant l'état `zi` plutôt que de refiltrer
    tout le tampon à chaque tour : c'est causal, cohérent d'un bloc à l'autre, et
    ça coûte O(n) au lieu de O(n × blocs).
    """

    def __init__(self, sos: np.ndarray):
        self.sos = np.asarray(sos, dtype=np.float64)
        self.zi = signal.sosfilt_zi(self.sos)
        self._primed = False

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64).ravel()
        if x.size == 0:
            return x.astype(np.float32)
        if not self._primed:
            # Amorçage sur la première valeur : évite le transitoire de plusieurs
            # secondes qu'on aurait avec un état nul sur un passe-bande à 0,1 Hz.
            self.zi = signal.sosfilt_zi(self.sos) * x[0]
            self._primed = True
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        return y.astype(np.float32)


class Decimator:
    """Décimation entière avec anti-repliement, état persistant entre blocs."""

    def __init__(self, factor: int, fs: float, order: int = 4):
        self.factor = max(1, int(factor))
        self.phase = 0
        if self.factor > 1:
            cutoff = 0.4 * fs / self.factor  # marge sous la nouvelle Nyquist
            self.lp = StreamingSOS(
                signal.butter(order, cutoff, "low", fs=fs, output="sos")
            )
        else:
            self.lp = None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).ravel()
        if self.factor == 1:
            return x
        y = self.lp(x)
        idx = np.arange(self.phase, y.size, self.factor)
        if idx.size:
            self.phase = int(idx[-1]) + self.factor - y.size
        else:
            self.phase -= y.size
        return y[idx]


class PacketGatedEnvelope:
    """I/Q brut → série d'amplitude en dB, en ne gardant que les paquets Wi-Fi.

    Le Wi-Fi est un illuminateur intermittent : entre deux paquets on ne mesure
    que le bruit du récepteur, qui ne contient aucune information sur la pièce.
    Moyenner aveuglément diluerait le signal dans le silence. On procède donc en
    trois étapes :

    1. intégration courte (``fine_decim`` échantillons ≈ 25 µs) pour lisser le
       bruit d'estimation de puissance sans écraser les paquets courts ;
    2. seuillage par rapport à un plancher de bruit suivi en continu (percentile
       bas + lissage exponentiel) — c'est le « gate » paquet ;
    3. moyenne des seules cases retenues à l'intérieur de chaque tick lent.

    Les ticks sans paquet suffisant sont comblés par maintien de la dernière
    valeur valide, et le taux de remplissage est remonté pour diagnostic.
    """

    def __init__(
        self,
        sample_rate: float,
        slow_rate: float,
        fine_decim: int,
        gate_db: float = 6.0,
        min_gated: int = 4,
        nf_alpha: float = 0.1,
        nf_percentile: float = 10.0,
    ):
        self.fine_decim = int(fine_decim)
        fine_rate = sample_rate / self.fine_decim
        ratio = fine_rate / slow_rate
        if abs(ratio - round(ratio)) > 1e-6:
            raise ValueError(
                f"sample_rate/fine_decim/slow_rate doit être entier "
                f"(obtenu {ratio:.4f}). Ajuste fine_decim ou slow_rate_hz."
            )
        self.per_slow = int(round(ratio))
        self.gate_lin = 10.0 ** (gate_db / 10.0)
        self.min_gated = int(min_gated)
        self.nf_alpha = float(nf_alpha)
        self.nf_percentile = float(nf_percentile)

        self.noise_floor: float | None = None
        self._last_db: float | None = None
        self._carry_iq = np.zeros(0, dtype=np.complex64)
        self._carry_p = np.zeros(0, dtype=np.float32)
        self._carry_g = np.zeros(0, dtype=bool)

    def process(self, iq: np.ndarray) -> tuple[np.ndarray, float]:
        """Retourne (série dB au rythme lent, fraction de ticks réellement mesurés)."""
        iq = np.asarray(iq, dtype=np.complex64).ravel()
        if self._carry_iq.size:
            iq = np.concatenate([self._carry_iq, iq])

        n = (iq.size // self.fine_decim) * self.fine_decim
        self._carry_iq = iq[n:].copy()
        if n == 0:
            return np.zeros(0, dtype=np.float32), 0.0

        # Puissance instantanée : .real/.imag sont des vues sur du complex64,
        # donc pas de copie ici.
        blk = iq[:n]
        p = blk.real.astype(np.float32) ** 2 + blk.imag.astype(np.float32) ** 2
        pf = p.reshape(-1, self.fine_decim).mean(axis=1)

        nf_now = float(np.percentile(pf, self.nf_percentile))
        if self.noise_floor is None:
            self.noise_floor = nf_now
        else:
            a = self.nf_alpha
            self.noise_floor = (1 - a) * self.noise_floor + a * nf_now
        gate = pf > (self.noise_floor * self.gate_lin)

        pf = np.concatenate([self._carry_p, pf])
        gate = np.concatenate([self._carry_g, gate])
        m = (pf.size // self.per_slow) * self.per_slow
        self._carry_p = pf[m:].copy()
        self._carry_g = gate[m:].copy()
        if m == 0:
            return np.zeros(0, dtype=np.float32), 0.0

        P = pf[:m].reshape(-1, self.per_slow)
        G = gate[:m].reshape(-1, self.per_slow)
        cnt = G.sum(axis=1)
        acc = np.where(G, P, 0.0).sum(axis=1)
        ok = cnt >= self.min_gated
        mean_p = acc / np.maximum(cnt, 1)
        db = 10.0 * np.log10(np.maximum(mean_p, EPS))

        out = np.empty(db.size, dtype=np.float32)
        last = self._last_db
        if last is None:
            last = float(db[ok][0]) if ok.any() else float(db[0])
        for i in range(db.size):
            if ok[i]:
                last = float(db[i])
            out[i] = last
        self._last_db = last

        return out, float(ok.mean())
