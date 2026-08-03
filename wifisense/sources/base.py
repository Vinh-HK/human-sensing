"""Contrat commun à toutes les sources de mesure.

Une source produit, pour chaque voie (`channel`), un dictionnaire de flux
temps-lent échantillonnés à `slow_rate_hz` :

  ``mean``    — gain moyen du canal, robuste, utilisé par la douche et la cuisine
  ``primary`` — flux le plus sensible aux micro-mouvements, utilisé pour la
                respiration. Avec un SDR c'est identique à ``mean`` (une seule
                mesure de puissance disponible) ; avec du CSI c'est la
                sous-porteuse de plus forte variance.

C'est cette abstraction qui permet de passer du LimeSDR aux nœuds ESP32 sans
toucher aux détecteurs, à la logique de présence ni au calibrage.
"""

from __future__ import annotations

import numpy as np


class Source:
    slow_rate_hz: float = 100.0
    channels: list[int] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def read(self) -> tuple[dict[int, dict[str, np.ndarray]], dict]:
        """Bloque jusqu'au bloc suivant. Retourne (données par voie, diagnostics)."""
        raise NotImplementedError

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
