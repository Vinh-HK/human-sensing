"""Source LimeSDR via SoapySDR.

Deux points de conception importants :

1. **Lecture dans un thread dédié.** Le flux USB n'attend pas : si on fait la DSP
   entre deux `readStream`, le FIFO du LimeSDR déborde et on perd des paquets
   (overflows « O » à répétition). Le thread lit en continu et pousse dans une
   file bornée ; si la DSP décroche, on jette des blocs et on le signale, plutôt
   que de laisser la latence grandir sans fin.

2. **Rythme lent = 100 Hz.** On n'a besoin ni de la bande passante ni des
   échantillons bruts au-delà de l'estimation de puissance. Toute la charge CPU
   est dans `PacketGatedEnvelope`, qui est en O(n) avec des opérations numpy
   vectorisées : à 2 Msps ça représente quelques pour cent d'un cœur.

Limite matérielle à connaître : le LimeSDR-USB monte à 3,8 GHz, donc **le Wi-Fi
5 GHz est hors de portée**. Il faut un SSID 2,4 GHz actif dans le logement.
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np

from ..dsp import PacketGatedEnvelope
from .base import Source

log = logging.getLogger(__name__)


class LimeSDRSource(Source):
    def __init__(self, cfg: dict, channels: list[int]):
        self.cfg = cfg
        self.channels = list(channels)
        self.slow_rate_hz = float(cfg.get("slow_rate_hz", 100.0))
        self.fs = float(cfg.get("sample_rate_hz", 2e6))
        self.block_s = float(cfg.get("block_s", 0.5))
        self.block_n = int(self.block_s * self.fs)

        self.env = {
            ch: PacketGatedEnvelope(
                self.fs,
                self.slow_rate_hz,
                int(cfg.get("fine_decim", 50)),
                float(cfg.get("gate_db", 6.0)),
                int(cfg.get("min_gated", 4)),
            )
            for ch in self.channels
        }

        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread = None
        self._dev = None
        self._stream = None
        self.overflows = 0
        self.drops = 0

    # --------------------------------------------------------------- device

    def start(self) -> None:
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

        args = dict(driver=self.cfg.get("driver", "lime"))
        extra = self.cfg.get("device_args") or ""
        for kv in [p for p in extra.split(",") if "=" in p]:
            k, v = kv.split("=", 1)
            args[k.strip()] = v.strip()

        log.info("Ouverture du SDR %s", args)
        self._dev = SoapySDR.Device(args)

        for ch in self.channels:
            self._dev.setSampleRate(SOAPY_SDR_RX, ch, self.fs)
            self._dev.setBandwidth(SOAPY_SDR_RX, ch, float(self.cfg.get("bandwidth_hz", 3e6)))
            self._dev.setFrequency(SOAPY_SDR_RX, ch, float(self.cfg.get("frequency_hz", 2.437e9)))
            self._dev.setGain(SOAPY_SDR_RX, ch, float(self.cfg.get("gain_db", 40.0)))
            ant = self.cfg.get("antenna")
            if ant:
                try:
                    self._dev.setAntenna(SOAPY_SDR_RX, ch, ant)
                except Exception:
                    log.warning(
                        "Antenne %r refusée sur la voie %d ; disponibles : %s",
                        ant, ch, self._dev.listAntennas(SOAPY_SDR_RX, ch),
                    )

        self._stream = self._dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, self.channels)
        self._dev.activateStream(self._stream)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        log.info(
            "LimeSDR actif : %.3f MHz, %.2f Msps, voies %s",
            float(self.cfg.get("frequency_hz", 2.437e9)) / 1e6,
            self.fs / 1e6,
            self.channels,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._dev is not None and self._stream is not None:
            try:
                self._dev.deactivateStream(self._stream)
                self._dev.closeStream(self._stream)
            except Exception:
                log.exception("Fermeture du flux SDR")

    # --------------------------------------------------------------- thread

    def _reader(self) -> None:
        from SoapySDR import SOAPY_SDR_OVERFLOW, SOAPY_SDR_TIMEOUT

        try:
            mtu = int(self._dev.getStreamMTU(self._stream))
        except Exception:
            mtu = 8192
        mtu = max(1024, min(mtu, 65536))

        nch = len(self.channels)
        chunk = [np.empty(mtu, dtype=np.complex64) for _ in range(nch)]
        acc = [[] for _ in range(nch)]
        have = 0

        while not self._stop.is_set():
            sr = self._dev.readStream(self._stream, chunk, mtu, timeoutUs=1000000)
            n = sr.ret
            if n == SOAPY_SDR_TIMEOUT:
                continue
            if n == SOAPY_SDR_OVERFLOW:
                self.overflows += 1
                continue
            if n < 0:
                log.warning("readStream a renvoyé %d", n)
                continue

            for i in range(nch):
                acc[i].append(chunk[i][:n].copy())
            have += n
            if have < self.block_n:
                continue

            block = {
                self.channels[i]: np.concatenate(acc[i])[: self.block_n]
                for i in range(nch)
            }
            acc = [[] for _ in range(nch)]
            have = 0
            try:
                self._q.put_nowait(block)
            except queue.Full:
                # On préfère perdre un bloc que d'accumuler du retard : mieux vaut
                # une détection à l'heure sur des données récentes.
                self.drops += 1
                if self.drops % 20 == 1:
                    log.warning(
                        "Traitement en retard sur le SDR : %d blocs jetés", self.drops
                    )

    # ----------------------------------------------------------------- read

    def read(self):
        block = self._q.get()
        out, diag = {}, {}
        for ch in self.channels:
            series, duty = self.env[ch].process(block[ch])
            out[ch] = {"mean": series, "primary": series}
            diag[ch] = {
                "duty": duty,
                "noise_floor_db": (
                    10 * np.log10(self.env[ch].noise_floor)
                    if self.env[ch].noise_floor
                    else None
                ),
            }
        return out, {
            "channels": diag,
            "overflows": self.overflows,
            "drops": self.drops,
        }
