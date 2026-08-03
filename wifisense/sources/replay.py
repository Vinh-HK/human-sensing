"""Rejeu d'un enregistrement temps-lent (.npz).

Enregistrer une séance puis la rejouer est de loin le moyen le plus efficace de
régler les seuils : on capture une fois « pièce vide », une fois « quelqu'un sous
la douche », une fois « lave-linge seul », et on peut ensuite itérer sur les
paramètres autant qu'on veut sans refaire la manip. Un enregistrement pèse
400 o/s par voie — on peut en garder des heures.
"""

from __future__ import annotations

import numpy as np

from .base import Source


class ReplaySource(Source):
    def __init__(self, cfg: dict, channels: list[int]):
        path = cfg.get("replay_path")
        if not path:
            raise ValueError("source.replay_path est requis pour le type 'replay'")
        data = np.load(path, allow_pickle=False)
        self.slow_rate_hz = float(data["slow_rate_hz"])
        self.block_s = float(cfg.get("block_s", 0.5))
        self.block_n = max(1, int(self.block_s * self.slow_rate_hz))
        self.channels = list(channels)

        self.streams: dict[int, dict[str, np.ndarray]] = {}
        for key in data.files:
            if "__" not in key:
                continue
            ch_s, stream = key.split("__", 1)
            ch = int(ch_s)
            self.streams.setdefault(ch, {})[stream] = data[key]

        missing = [c for c in self.channels if c not in self.streams]
        if missing:
            raise ValueError(
                f"Voies absentes de l'enregistrement {path} : {missing} "
                f"(présentes : {sorted(self.streams)})"
            )
        self.n = min(len(v["mean"]) for v in self.streams.values())
        self.pos = 0

    def read(self):
        if self.pos >= self.n:
            raise StopIteration("fin de l'enregistrement")
        lo, hi = self.pos, min(self.pos + self.block_n, self.n)
        self.pos = hi
        out = {
            ch: {k: v[lo:hi] for k, v in self.streams[ch].items()}
            for ch in self.channels
        }
        return out, {"channels": {ch: {"duty": 1.0} for ch in self.channels},
                     "replay_progress": self.pos / self.n}
