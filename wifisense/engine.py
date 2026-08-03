"""Boucle principale : source → détecteurs → présence → ampoules."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import baseline_for
from .detectors import Detection, build_detector
from .drift import DriftMonitor
from .features import FeatureBank
from .localizer import ClassifierZone, RoomClassifier
from .presence import PresenceLogic
from .sources import build_source
from .zigbee import ZigbeeLights

log = logging.getLogger(__name__)


class TrafficGenerator:
    """Illumine le canal en générant du trafic Wi-Fi.

    Le radar passif n'a de signal que quand quelqu'un émet. Or le Wi-Fi
    domestique est très intermittent : hors trafic, on ne mesure que le bruit du
    récepteur. On envoie donc un flux UDP léger vers un appareil **connecté en
    Wi-Fi** — c'est la traversée air entre le point d'accès et cet appareil qui
    illumine la pièce. Viser le routeur depuis un serveur câblé ne produirait
    aucune onde : les paquets resteraient sur le cuivre.

    Le port 9 (discard) est utilisé par défaut : la cible le rejette, mais le
    paquet a bien traversé l'air, ce qui est tout ce qui nous intéresse.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.target = cfg.get("target") or ""
        self.port = int(cfg.get("port", 9))
        self.rate = float(cfg.get("rate_hz", 100.0))
        self.payload = b"\x00" * int(cfg.get("payload_bytes", 200))
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if not self.cfg.get("enabled") or not self.target:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info(
            "Générateur de trafic : %.0f paquets/s vers %s:%d",
            self.rate, self.target, self.port,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        period = 1.0 / max(self.rate, 1.0)
        nxt = time.monotonic()
        while not self._stop.is_set():
            try:
                sock.sendto(self.payload, (self.target, self.port))
            except OSError as exc:
                log.warning("Envoi de trafic impossible : %s", exc)
                time.sleep(1.0)
                continue
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()
        sock.close()


@dataclass
class ZoneRuntime:
    name: str
    cfg: dict
    channel: int
    detector: object
    presence: PresenceLogic
    last: Optional[Detection] = None
    state: bool = False


class Engine:
    def __init__(
        self,
        cfg: dict,
        calib: dict | None = None,
        dry_run: bool = False,
        control_lights: bool = True,
    ):
        self.cfg = cfg
        self.calib = calib or {}
        self.control_lights = control_lights

        channels = sorted({int(z["channel"]) for z in cfg["zones"].values() if z["enabled"]})
        if not channels:
            raise ValueError("Aucune zone activée")
        self.source = build_source(cfg["source"], channels)
        fs = self.source.slow_rate_hz

        # Banc de caractéristiques multi-nœuds : toujours construit, car il est
        # aussi ce que le calibrage enregistre pour entraîner le classifieur.
        ccfg = cfg.get("classifier", {})
        self.bank = FeatureBank(
            channels, {c: fs for c in channels},
            float(ccfg.get("window_s", 8.0)), int(ccfg.get("n_bands", 6)),
        )
        self.clf = self._load_classifier(ccfg)

        pcfg = cfg["presence"]
        self.zones: list[ZoneRuntime] = []
        for name, z in cfg["zones"].items():
            if not z["enabled"]:
                continue
            base = baseline_for(self.calib, name)
            if z["detector"] == "classifier":
                det = ClassifierZone(name, z, base)
                if self.clf is not None and name not in self.clf.rooms:
                    log.warning(
                        "Zone %r en mode classifieur mais absente du modèle "
                        "(pièces connues : %s). Elle restera éteinte.",
                        name, ", ".join(self.clf.rooms),
                    )
            else:
                det = build_detector(z["detector"], fs, z, base)
            self.zones.append(
                ZoneRuntime(
                    name=name,
                    cfg=z,
                    channel=int(z["channel"]),
                    detector=det,
                    presence=PresenceLogic(
                        int(z.get("on_confirm", pcfg["on_confirm"])),
                        float(z.get("hold_s", pcfg["hold_s"])),
                    ),
                )
            )

        self.lights = ZigbeeLights(cfg["mqtt"], dry_run=dry_run or not control_lights)
        self.traffic = TrafficGenerator(cfg.get("traffic", {}))
        self._log_path = cfg.get("decision_log") or ""
        self.dcfg = cfg.get("drift", {})
        self.drift = (
            DriftMonitor(self.clf, self.dcfg) if self.clf is not None else None
        )
        self.alerts: list = []
        self._stop = threading.Event()

    def _load_classifier(self, ccfg: dict):
        needed = any(
            z["detector"] == "classifier" for z in self.cfg["zones"].values()
            if z["enabled"]
        )
        path = ccfg.get("model_path") or ""
        if not needed:
            return None
        if not path or not os.path.exists(path):
            log.warning(
                "Zones en mode classifieur mais modèle absent (%s). "
                "Calibre les pièces puis lance : wifisense fit", path or "(non défini)",
            )
            return None
        clf = RoomClassifier.load(path)
        log.info("Classifieur chargé : %s (pièces : %s)", path, ", ".join(clf.rooms))
        return clf

    def _flush_collected(self) -> None:
        """Verse les échantillons « vide » auto-collectés dans le jeu de données.

        On enregistre, on ne réentraîne pas. Un changement de modèle doit rester
        un acte délibéré : si la période « calme » contenait en réalité quelqu'un
        d'endormi, un réentraînement automatique détruirait silencieusement le
        système. `fit` reste à la main de l'utilisateur.
        """
        if self.drift is None or not self.drift.auto_collect:
            return
        feats = self.drift.take_collected()
        if feats is None or feats.size == 0:
            return
        from .calibrate import dataset_append

        path = self.cfg["classifier"]["dataset_path"]
        try:
            dataset_append(path, "empty", feats, replace=False)
            log.info(
                "%d échantillons « vide » ajoutés à %s. Lance `wifisense fit` "
                "pour en tenir compte.", len(feats), path,
            )
        except OSError as exc:
            log.warning("Collecte automatique non écrite : %s", exc)

    def _trace(self, t: float, z: "ZoneRuntime", det: Detection) -> None:
        """Journalise chaque bascule avec ce qui l'a provoquée.

        Idée reprise de WorldGraph : une décision doit pouvoir être remontée à sa
        preuve. Sans ça, régler un seuil revient à deviner pourquoi la lumière
        s'est allumée il y a trois heures.
        """
        thr = z.detector.baseline_db + z.detector.threshold_db
        log.info(
            "%s -> %s (score %.2f dB vs seuil %.2f ; %s)",
            z.name, "ON" if z.state else "OFF", det.score_db, thr, det.info or {},
        )
        if not self._log_path:
            return
        rec = {
            "t": round(t, 2),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "zone": z.name,
            "state": "ON" if z.state else "OFF",
            "score_db": round(det.score_db, 2),
            "threshold_db": round(thr, 2),
            "detector": z.cfg["detector"],
            "info": dict(det.info or {}),
        }
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Journal des décisions non écrit : %s", exc)

    def stop(self) -> None:
        self._stop.set()

    def run(
        self,
        duration_s: float | None = None,
        on_update: Callable[[float, list[ZoneRuntime], dict], None] | None = None,
    ) -> None:
        names = [n for z in self.zones for n in z.cfg["lights"]]
        self.lights.register(names)
        self.lights.connect()
        self.traffic.start()
        self.source.start()

        t0 = time.monotonic()
        try:
            while not self._stop.is_set():
                data, diag = self.source.read()
                now = time.monotonic()

                feat = self.bank.update(data)
                diag["features"] = feat
                room_scores = (
                    self.clf.scores(feat)
                    if (self.clf is not None and feat is not None)
                    else {}
                )

                for z in self.zones:
                    if isinstance(z.detector, ClassifierZone):
                        if z.name not in room_scores:
                            continue
                        s = room_scores[z.name]
                        det = Detection(
                            s,
                            s > z.detector.baseline_db + z.detector.threshold_db,
                            {"fisher": round(self.clf.fisher.get(z.name, 0.0), 2)},
                        )
                    else:
                        streams = data.get(z.channel)
                        if not streams:
                            continue
                        x = streams.get(z.detector.STREAM)
                        if x is None or len(x) == 0:
                            continue
                        # presence_hint gèle l'apprentissage du fond pendant
                        # qu'une présence est affirmée (cf. KitchenDetector).
                        det = z.detector.update(x, presence_hint=z.state)
                        if det is None:
                            continue

                    z.last = det
                    was = z.state
                    z.state = z.presence.update(det.detected, now)
                    if z.state != was:
                        self._trace(now - t0, z, det)
                    if self.control_lights:
                        self.lights.apply(z.cfg, z.state)

                if self.drift is not None:
                    for alert in self.drift.update(now, feat, room_scores):
                        self.alerts.append(alert)
                        self.lights.publish_status(
                            self.dcfg.get("status_topic", ""), alert.as_dict()
                        )
                    diag["alerts"] = self.alerts[-5:]
                    self._flush_collected()

                if on_update is not None:
                    on_update(now - t0, self.zones, diag)
                if duration_s is not None and (now - t0) >= duration_s:
                    break
        finally:
            self.source.stop()
            self.traffic.stop()
            self.lights.close()
