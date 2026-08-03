"""Source ESP32-S3 / ESP32-C6 : CSI reçu en UDP.

Format de trame (RuView ADR-018, little-endian) :

    offset  taille  champ
    0       4       magic 0xC5110001
    4       1       node_id           → sert d'identifiant de voie/pièce
    5       1       nombre d'antennes
    6       2       nombre de sous-porteuses
    8       4       fréquence (MHz)
    12      4       numéro de séquence
    16      1       RSSI (dBm, signé)
    17      1       plancher de bruit (dBm, signé)
    18      2       réservé
    20      N*2     paires I/Q en int8, par sous-porteuse, par antenne

Pourquoi c'est meilleur qu'un SDR pour cet usage : chaque paquet apporte 56
mesures complexes indépendantes au lieu d'un scalaire de puissance. Les
sous-porteuses ne réagissent pas de la même manière à un déplacement donné (leurs
longueurs d'onde diffèrent), donc il y en a presque toujours une qui est dans une
zone de sensibilité favorable. C'est ce qui rend la respiration détectable en
pratique là où l'amplitude large bande ne voit rien.

Une trame CSI n'est produite que lorsqu'un paquet est reçu : le rythme est donc
irrégulier (~20 Hz typique). On rééchantillonne sur une grille régulière, en
maintenant la dernière valeur pour les cases vides.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

import numpy as np

from .base import Source

log = logging.getLogger(__name__)

MAGIC = 0xC5110001
HEADER = struct.Struct("<IBBHIIbbH")
assert HEADER.size == 20


class _NodeState:
    """Accumulateur par nœud : sélection de sous-porteuse + mise en grille."""

    def __init__(self, slow_rate: float, refresh_s: float):
        self.slow_rate = slow_rate
        self.refresh_s = refresh_s
        self.pending: list[tuple[float, float, np.ndarray]] = []
        self.mu: np.ndarray | None = None      # moyenne EMA par sous-porteuse
        self.var: np.ndarray | None = None     # variance EMA par sous-porteuse
        self.best = 0
        self.last_pick = 0.0
        self.last_mean = None
        self.last_primary = None
        self.grid_t: float | None = None
        self.last_frame_t = 0.0

    def add(self, t: float, amp: np.ndarray) -> None:
        # Les sous-porteuses nulles (DC, bandes de garde) sont toujours à zéro :
        # les inclure diluerait la moyenne et polluerait la sélection.
        valid = amp > 0
        if valid.sum() < 4:
            return
        db = 20.0 * np.log10(np.maximum(amp, 1e-3))

        a = 0.02
        if self.mu is None or self.mu.shape != db.shape:
            self.mu = db.copy()
            self.var = np.zeros_like(db)
        else:
            d = db - self.mu
            self.mu += a * d
            self.var = (1 - a) * self.var + a * d * d

        if t - self.last_pick > self.refresh_s:
            v = np.where(valid, self.var, -np.inf)
            self.best = int(np.argmax(v))
            self.last_pick = t

        self.pending.append((t, float(db[valid].mean()), db))
        self.last_frame_t = t

    def drain(self, now: float):
        """Rend les échantillons de la grille régulière échus, sous forme de séries."""
        if self.grid_t is None:
            if not self.pending:
                return np.zeros(0, np.float32), np.zeros(0, np.float32), 0.0
            self.grid_t = self.pending[0][0]

        dt = 1.0 / self.slow_rate
        means, prims, filled, total = [], [], 0, 0
        while self.grid_t + dt <= now:
            lo, hi = self.grid_t, self.grid_t + dt
            bucket = [p for p in self.pending if lo <= p[0] < hi]
            if bucket:
                m = float(np.mean([b[1] for b in bucket]))
                p = float(np.mean([b[2][self.best] for b in bucket]))
                self.last_mean, self.last_primary = m, p
                filled += 1
            elif self.last_mean is not None:
                m, p = self.last_mean, self.last_primary
            else:
                self.grid_t = hi
                continue
            means.append(m)
            prims.append(p)
            total += 1
            self.grid_t = hi

        if self.pending:
            self.pending = [p for p in self.pending if p[0] >= self.grid_t - dt]
        duty = (filled / total) if total else 0.0
        return (
            np.asarray(means, np.float32),
            np.asarray(prims, np.float32),
            duty,
        )


class Esp32CsiSource(Source):
    def __init__(self, cfg: dict, channels: list[int]):
        self.cfg = cfg
        self.channels = list(channels)
        self.slow_rate_hz = float(cfg.get("slow_rate_hz", 20.0))
        self.block_s = float(cfg.get("block_s", 0.5))
        self.timeout_s = float(cfg.get("csi_timeout_s", 10.0))
        self.nodes = {
            ch: _NodeState(self.slow_rate_hz, float(cfg.get("subcarrier_refresh_s", 20.0)))
            for ch in self.channels
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None
        self._thread = None
        self.bad_frames = 0
        self.unknown_nodes: set[int] = set()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._sock.bind((self.cfg.get("udp_host", "0.0.0.0"), int(self.cfg.get("udp_port", 5566))))
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        log.info(
            "Écoute CSI UDP sur %s:%s, nœuds attendus %s",
            self.cfg.get("udp_host"), self.cfg.get("udp_port"), self.channels,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            self._sock.close()

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self._ingest(data, time.monotonic())

    def _ingest(self, data: bytes, t: float) -> None:
        if len(data) < HEADER.size:
            self.bad_frames += 1
            return
        magic, node, nant, nsub, _freq, _seq, _rssi, _nf, _rsv = HEADER.unpack_from(data)
        if magic != MAGIC or nsub == 0 or nsub > 512 or nant == 0:
            self.bad_frames += 1
            return
        need = HEADER.size + nant * nsub * 2
        if len(data) < need:
            self.bad_frames += 1
            return
        if node not in self.nodes:
            if node not in self.unknown_nodes:
                self.unknown_nodes.add(node)
                log.warning("Trames CSI reçues du nœud %d, non déclaré en config", node)
            return

        iq = np.frombuffer(data, dtype=np.int8, count=nant * nsub * 2, offset=HEADER.size)
        iq = iq.reshape(nant, nsub, 2).astype(np.float32)
        # On n'utilise que l'antenne 0 : l'ESP32 n'en a qu'une, et sur les cartes
        # multi-antennes les voies ne sont pas synchronisées en phase.
        amp = np.hypot(iq[0, :, 0], iq[0, :, 1])
        with self._lock:
            self.nodes[node].add(t, amp)

    def read(self):
        time.sleep(self.block_s)
        now = time.monotonic()
        out, diag = {}, {}
        with self._lock:
            for ch, st in self.nodes.items():
                m, p, duty = st.drain(now)
                out[ch] = {"mean": m, "primary": p}
                stale = now - st.last_frame_t if st.last_frame_t else None
                diag[ch] = {
                    "duty": duty,
                    "subcarrier": st.best,
                    "silent_s": stale,
                }
                if stale is not None and stale > self.timeout_s:
                    log.warning("Nœud %d silencieux depuis %.0f s", ch, stale)
        return out, {"channels": diag, "bad_frames": self.bad_frames}
