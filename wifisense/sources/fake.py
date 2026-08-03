"""Simulateur : génère de l'I/Q Wi-Fi synthétique, sans matériel.

Sert à trois choses : valider toute la chaîne (gate paquet → détecteurs →
présence → MQTT) avant d'avoir branché quoi que ce soit, faire tourner
`selftest`, et régler les seuils hors ligne.

Le modèle est volontairement simple mais pas naïf : on génère de vrais paquets
intermittents (arrivées poissonniennes) dont la puissance est modulée par un
gain de canal dépendant du scénario. C'est exactement la structure que le gate
paquet doit savoir exploiter.
"""

from __future__ import annotations

import time

import numpy as np
from scipy import signal

from ..dsp import PacketGatedEnvelope, StreamingSOS
from .base import Source

SCENARIOS = (
    "empty",
    "shower",
    "breathing",
    "walking",
    "machine",
    "machine_human",
)


class _GainModel:
    """Gain du canal au rythme lent, avec état persistant entre les blocs."""

    def __init__(self, scenario: str, slow_rate: float, rng: np.random.Generator):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"Scénario inconnu : {scenario!r} (attendu : {list(SCENARIOS)})"
            )
        self.scenario = scenario
        self.fs = slow_rate
        self.rng = rng
        self.t = 0.0

        def bp(lo, hi):
            hi = min(hi, 0.45 * slow_rate)
            return StreamingSOS(
                signal.butter(2, [lo, hi], "bandpass", fs=slow_rate, output="sos")
            )

        self.motion = bp(0.5, 8.0)     # corps qui bouge : large bande, lent
        self.splash = bp(1.0, 25.0)    # eau : plus large et plus fort
        self._warm = False

    def __call__(self, n: int) -> np.ndarray:
        t = self.t + np.arange(n) / self.fs
        self.t += n / self.fs
        g = np.ones(n, dtype=np.float64)
        w = self.rng.standard_normal(n)

        if self.scenario == "shower":
            g += 0.70 * self.splash(w)
        elif self.scenario == "breathing":
            # ~15 cycles/min, amplitude réaliste : quelques pour-cent du gain
            g += 0.020 * np.sin(2 * np.pi * 0.25 * t)
            g += 0.003 * w
        elif self.scenario == "walking":
            g += 0.50 * self.motion(w)
        elif self.scenario == "machine":
            g += 0.20 * np.sin(2 * np.pi * 23.0 * t)
            g += 0.10 * np.sin(2 * np.pi * 46.0 * t)
            g += 0.005 * w
        elif self.scenario == "machine_human":
            g += 0.20 * np.sin(2 * np.pi * 23.0 * t)
            g += 0.10 * np.sin(2 * np.pi * 46.0 * t)
            g += 0.30 * self.motion(w)
        else:  # empty
            g += 0.002 * w

        if not self._warm:
            # Les filtres mettent quelques secondes à s'établir : on ne veut pas
            # que ce transitoire soit interprété comme du mouvement.
            self._warm = True
        return np.clip(g, 0.05, None)


class _WorldGain:
    """Modèle d'appartement : plusieurs occupants, plusieurs nœuds.

    Chaque pièce possède un vecteur de couplage vers les nœuds — fort pour son
    propre nœud, plus faible pour les autres. C'est cette fuite volontaire qui
    rend le test réaliste : dans 42 m² les cloisons ne bloquent rien, et un
    classifieur qui ne serait validé que sur des pièces parfaitement isolées ne
    prouverait rien.

    Le mouvement d'un occupant est **corrélé** entre les nœuds (c'est un seul
    corps qui bouge) mais pas identique : chaque nœud voit une combinaison d'une
    composante commune et d'une composante propre.
    """

    SHARED = 0.7  # part commune du mouvement d'un occupant entre les nœuds
    AMP = 0.45

    def __init__(self, rooms: dict, occupants, channels, slow_rate, rng,
                 machine_node=None, max_slots: int = 6):
        self.rooms = {k: np.asarray(v, dtype=np.float64) for k, v in rooms.items()}
        for name, v in self.rooms.items():
            if v.size != len(channels):
                raise ValueError(
                    f"Pièce {name!r} : {v.size} couplages pour {len(channels)} nœuds"
                )
        self.channels = list(channels)
        self.fs = slow_rate
        self.rng = rng
        self.machine_node = machine_node
        self.t = 0.0
        self.occupants = list(occupants)

        def bp():
            hi = min(8.0, 0.45 * slow_rate)
            return StreamingSOS(
                signal.butter(2, [0.5, hi], "bandpass", fs=slow_rate, output="sos")
            )

        # Filtres pré-alloués par emplacement d'occupant : les occupants peuvent
        # changer de pièce en cours de route sans réinitialiser l'état.
        self.shared = [bp() for _ in range(max_slots)]
        self.indep = [[bp() for _ in self.channels] for _ in range(max_slots)]
        self.max_slots = max_slots

    def set_rooms(self, rooms: dict) -> None:
        """Change la matrice de couplage : simule un réaménagement du logement."""
        self.rooms = {k: np.asarray(v, dtype=np.float64) for k, v in rooms.items()}

    def set_occupants(self, occupants) -> None:
        if len(occupants) > self.max_slots:
            raise ValueError(f"Au plus {self.max_slots} occupants simulés")
        for r in occupants:
            if r not in self.rooms:
                raise ValueError(f"Pièce inconnue : {r!r} (connues : {sorted(self.rooms)})")
        self.occupants = list(occupants)

    def __call__(self, n: int) -> dict[int, np.ndarray]:
        t = self.t + np.arange(n) / self.fs
        self.t += n / self.fs
        out = {
            ch: 1.0 + 0.002 * self.rng.standard_normal(n) for ch in self.channels
        }

        for slot, room in enumerate(self.occupants):
            coupling = self.rooms[room]
            m = self.shared[slot](self.rng.standard_normal(n))
            for i, ch in enumerate(self.channels):
                ind = self.indep[slot][i](self.rng.standard_normal(n))
                motion = self.SHARED * m + (1.0 - self.SHARED) * ind
                out[ch] += self.AMP * float(coupling[i]) * motion

        if self.machine_node is not None and self.machine_node in out:
            out[self.machine_node] += (
                0.20 * np.sin(2 * np.pi * 23.0 * t)
                + 0.10 * np.sin(2 * np.pi * 46.0 * t)
            )
        return {ch: np.clip(v, 0.05, None) for ch, v in out.items()}


class FakeSource(Source):
    def __init__(self, cfg: dict, channels: list[int]):
        self.channels = list(channels)
        self.slow_rate_hz = float(cfg.get("slow_rate_hz", 100.0))
        self.fs = float(cfg.get("sample_rate_hz", 500e3))
        self.block_s = float(cfg.get("block_s", 0.5))
        self.speed = float(cfg.get("fake_speed", 1.0))

        ratio = self.fs / self.slow_rate_hz
        if abs(ratio - round(ratio)) > 1e-6:
            raise ValueError("sample_rate_hz doit être un multiple de slow_rate_hz")
        self.upsample = int(round(ratio))

        rng = np.random.default_rng(int(cfg.get("fake_seed", 12345)))
        self.rng = rng

        # Deux modes : scénarios indépendants par voie (mise au point des
        # détecteurs), ou monde partagé multi-occupants (mise au point du
        # classifieur, où la corrélation entre nœuds est justement l'objet).
        world = cfg.get("fake_world")
        if world:
            self.world = _WorldGain(
                world["rooms"], world.get("occupants", []), self.channels,
                self.slow_rate_hz, rng, world.get("machine_node"),
            )
            self.gains = None
        else:
            self.world = None
            scen = cfg.get("fake_scenario", "empty")
            scen_map = (
                {int(k): v for k, v in scen.items()}
                if isinstance(scen, dict)
                else {ch: scen for ch in self.channels}
            )
            self.gains = {
                ch: _GainModel(scen_map.get(ch, "empty"), self.slow_rate_hz, rng)
                for ch in self.channels
            }
        self.env = {
            ch: PacketGatedEnvelope(
                self.fs,
                self.slow_rate_hz,
                int(cfg.get("fine_decim", 25)),
                float(cfg.get("gate_db", 6.0)),
                int(cfg.get("min_gated", 4)),
            )
            for ch in self.channels
        }

        self.pps = float(cfg.get("fake_packets_per_s", 400.0))
        self.pkt_len = max(1, int(200e-6 * self.fs))  # paquet ~200 µs
        self.snr_lin = 10.0 ** (float(cfg.get("fake_snr_db", 25.0)) / 10.0)
        self._next = None

    def start(self):
        self._next = time.monotonic()

    def set_scenario(self, channel: int, scenario: str) -> None:
        """Change le scénario en cours de route (utilisé par `selftest`)."""
        if self.world is not None:
            raise RuntimeError("Source en mode monde : utilise set_occupants()")
        self.gains[channel] = _GainModel(scenario, self.slow_rate_hz, self.rng)

    def set_occupants(self, occupants) -> None:
        """Déplace les occupants simulés (mode monde)."""
        if self.world is None:
            raise RuntimeError("Source en mode scénario : utilise set_scenario()")
        self.world.set_occupants(occupants)

    def set_rooms(self, rooms: dict) -> None:
        """Réaménage le logement simulé (test du moniteur de dérive)."""
        if self.world is None:
            raise RuntimeError("Source en mode scénario : pas de monde à réaménager")
        self.world.set_rooms(rooms)

    def _make_iq(self, ch: int, n: int, gain_slow: np.ndarray) -> np.ndarray:
        g = np.repeat(gain_slow, self.upsample)
        if g.size < n:
            g = np.pad(g, (0, n - g.size), mode="edge")

        mask = np.zeros(n, dtype=bool)
        n_pkt = self.rng.poisson(self.pps * n / self.fs)
        if n_pkt:
            starts = self.rng.integers(0, max(1, n - self.pkt_len), size=n_pkt)
            for s in starts:
                mask[s : s + self.pkt_len] = True

        noise = (
            self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)
        ).astype(np.complex64) * np.float32(np.sqrt(0.5))
        sig = (
            self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)
        ).astype(np.complex64) * np.float32(np.sqrt(self.snr_lin / 2.0))
        sig *= (g * mask).astype(np.float32)
        return noise + sig

    def read(self):
        n = int(self.block_s * self.fs)
        n -= n % self.upsample
        n_slow = n // self.upsample

        if self.world is not None:
            gains = self.world(n_slow)
        else:
            gains = {ch: self.gains[ch](n_slow) for ch in self.channels}

        out, diag = {}, {}
        for ch in self.channels:
            iq = self._make_iq(ch, n, gains[ch])
            series, duty = self.env[ch].process(iq)
            out[ch] = {"mean": series, "primary": series}
            diag[ch] = {"duty": duty}

        if self.speed <= 1.0:
            self._next += self.block_s / max(self.speed, 1e-6)
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                self._next = time.monotonic()
        return out, {"channels": diag}
