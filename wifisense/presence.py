"""Lissage temporel de la décision brute des détecteurs.

Le but du système est d'éviter le défaut classique du détecteur de mouvement :
la lumière qui s'éteint parce qu'on n'a pas bougé. La logique est donc
asymétrique — prudente pour allumer, très lente pour éteindre.
"""

from __future__ import annotations


class PresenceLogic:
    def __init__(self, on_confirm: int = 2, hold_s: float = 120.0):
        self.on_confirm = max(1, int(on_confirm))
        self.hold_s = float(hold_s)
        self.state = False
        self.last_seen = 0.0
        self._streak = 0

    def update(self, detected: bool, now: float) -> bool:
        if detected:
            self._streak += 1
        else:
            self._streak = 0

        if self._streak >= self.on_confirm:
            # Toute détection confirmée réarme le compte à rebours d'extinction.
            self.last_seen = now
            self.state = True
        elif self.state and (now - self.last_seen) > self.hold_s:
            self.state = False
        return self.state

    def remaining_s(self, now: float) -> float:
        if not self.state:
            return 0.0
        return max(0.0, self.hold_s - (now - self.last_seen))
