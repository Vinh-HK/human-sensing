"""Extraction de caractéristiques multi-liens pour la classification par pièce.

Idée reprise de l'exemple MathWorks « Detect Human Presence Using Wireless
Sensing » : on retire la composante continue puis on regarde le **profil
Doppler**. Le retrait du DC élimine les trajets statiques (murs, meubles), qui
dominent le signal en amplitude mais ne portent aucune information de présence.
Ce qui reste est la partie variable du canal, c'est-à-dire ce qui bouge.

MathWorks fait une FFT 2D sur (sous-porteuses × paquets) : l'axe sous-porteuses
donne le domaine des retards, l'axe paquets le domaine Doppler. Ici on n'utilise
que l'axe Doppler, pour une raison pratique — le format de trame ESP32 ne
transporte pas la MAC de l'émetteur, et la version scalaire fonctionne avec
n'importe quelle source (SDR compris). L'enrichissement par sous-porteuses est
une extension naturelle une fois du vrai CSI disponible.

Sortie : par nœud, l'énergie en dB dans K bandes espacées logarithmiquement.
L'espacement log est délibéré — la respiration (0,2 Hz), la marche (1–5 Hz) et
les machines (15–40 Hz) sont séparées par des rapports, pas par des écarts.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from .dsp import EPS, RingBuffer


def band_edges(fs: float, lo: float = 0.2, n_bands: int = 6) -> np.ndarray:
    """Bornes de bandes espacées logarithmiquement, bornées par Nyquist."""
    hi = min(20.0, 0.45 * fs)
    if hi <= lo * 1.5:
        # Source très lente (CSI à ~20 Hz) : on garde ce qui reste exploitable.
        hi = max(lo * 2.0, 0.45 * fs)
    return np.geomspace(lo, hi, n_bands + 1)


class LinkFeatures:
    """Profil Doppler glissant d'un flux temps-lent (un nœud = un flux)."""

    def __init__(self, fs: float, window_s: float = 8.0, n_bands: int = 6,
                 lo_hz: float = 0.2):
        self.fs = float(fs)
        self.n_bands = int(n_bands)
        self.ring = RingBuffer(int(window_s * fs))
        self.edges = band_edges(self.fs, lo_hz, self.n_bands)
        self._nperseg = None

    @property
    def ready(self) -> bool:
        return self.ring.ready

    def update(self, x: np.ndarray) -> np.ndarray | None:
        self.ring.push(x)
        if not self.ring.ready:
            return None
        d = self.ring.data()
        if self._nperseg is None:
            n = 32
            while n * 2 <= min(d.size, int(round(4.0 * self.fs))):
                n *= 2
            self._nperseg = n
        # detrend='constant' = retrait du DC : c'est lui qui supprime les
        # trajets statiques et rend l'empreinte comparable d'un jour à l'autre.
        f, P = signal.welch(
            d, fs=self.fs, nperseg=self._nperseg,
            noverlap=self._nperseg // 2, detrend="constant",
        )
        out = np.empty(self.n_bands, dtype=np.float64)
        for i in range(self.n_bands):
            m = (f >= self.edges[i]) & (f < self.edges[i + 1])
            out[i] = 10.0 * np.log10(P[m].sum() + EPS) if m.any() else -200.0
        return out


class FeatureBank:
    """Concatène les profils de tous les nœuds en un unique vecteur."""

    def __init__(self, channels, fs_by_channel: dict[int, float],
                 window_s: float = 8.0, n_bands: int = 6):
        self.channels = list(channels)
        self.n_bands = int(n_bands)
        self.links = {
            ch: LinkFeatures(fs_by_channel[ch], window_s, n_bands)
            for ch in self.channels
        }

    @property
    def names(self) -> list[str]:
        return [
            f"ch{ch}_b{i}" for ch in self.channels for i in range(self.n_bands)
        ]

    @property
    def dim(self) -> int:
        return len(self.channels) * self.n_bands

    def update(self, data: dict[int, dict[str, np.ndarray]]) -> np.ndarray | None:
        """Rend le vecteur complet, ou None tant qu'un nœud n'est pas prêt."""
        parts = []
        for ch in self.channels:
            streams = data.get(ch)
            x = None if streams is None else streams.get("mean")
            v = self.links[ch].update(
                x if x is not None else np.zeros(0, dtype=np.float32)
            )
            if v is None:
                return None
            parts.append(v)
        return np.concatenate(parts)
