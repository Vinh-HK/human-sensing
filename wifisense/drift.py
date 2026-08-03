"""Surveillance de la dérive du modèle.

Un classifieur d'empreintes se périme. Pas brutalement — progressivement, et
sans rien signaler. Le but de ce module est que le système dise lui-même « je ne
suis plus fiable », au lieu que ça se découvre parce que la lumière fait
n'importe quoi depuis trois semaines.

Deux dérives distinctes, qu'il faut mesurer séparément parce qu'elles n'ont ni
la même cause ni le même remède :

**1. Dérive de référence** — mesurée pendant les périodes d'inoccupation. À vide,
tous les scores doivent tourner autour de 0 dB. S'ils s'en écartent, c'est le
canal lui-même qui a changé : nœud tombé, gain qui a bougé, trafic effondré,
nouveau brouilleur. Remède : vérifier l'installation.

**2. Dérive d'attribution** — mesurée pendant les périodes d'activité. C'est
celle qui attrape les meubles déplacés, et il faut bien voir pourquoi la
première ne suffit pas : appartement vide, il n'y a aucun mouvement avant comme
après le déménagement du canapé, donc le score reste à 0 dB des deux côtés.
Ce qui a changé, c'est la correspondance « personne dans le salon → empreinte ».
Elle ne se voit que quand quelqu'un est là. Symptôme : le meilleur score de
pièce s'affaisse vers le seuil, ou deux pièces se disputent la présence.
Remède : recalibrer la pièce concernée.

Le juge de « ça bouge ou pas » est `RoomClassifier.activity()`, une simple
distance au centroïde de la classe vide — délibérément **indépendante** des
discriminants. Utiliser le classifieur pour décider quand évaluer le
classifieur serait circulaire.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class DriftAlert:
    kind: str        # "baseline" | "attribution" | "coverage"
    severity: str    # "info" | "warn" | "recalibrate"
    message: str
    value: float
    n: int

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "severity": self.severity,
            "message": self.message, "value": round(self.value, 2), "n": self.n,
        }


class DriftMonitor:
    def __init__(self, clf, cfg: dict):
        self.clf = clf
        self.enabled = bool(cfg.get("enabled", True))
        self.report_every_s = float(cfg.get("report_every_s", 3600.0))
        self.min_samples = int(cfg.get("min_samples", 60))
        self.warn_db = float(cfg.get("warn_db", 2.5))
        self.recal_db = float(cfg.get("recalibrate_db", 5.0))
        self.degrade_db = float(cfg.get("degrade_db", 3.0))
        self.auto_collect = bool(cfg.get("auto_collect", False))
        self.max_auto = int(cfg.get("max_auto_samples", 400))

        refs = getattr(clf, "refs", {}) or {}
        self.act_empty = float(refs.get("activity_empty_p95", 0.0))
        self.act_occ = float(refs.get("activity_occupied_p50", 0.0))
        self.peak_ref = float(refs.get("peak_score_p50", 10.0))
        # Référence = médiane d'entraînement, comparée à la médiane de
        # fonctionnement. Comparer une médiane à un p95 mélangeait deux
        # statistiques et rendait le seuil arbitraire.
        self.nov_ref = float(refs.get("novelty_p50", 0.0))
        self.nov_warn = float(cfg.get("novelty_warn_ratio", 1.4))
        self.nov_recal = float(cfg.get("novelty_recalibrate_ratio", 2.0))
        # Seuil d'activité à mi-chemin entre les deux références : au-dessus, on
        # est sûr que quelque chose bouge, sans avoir consulté les discriminants.
        self.act_gate = (
            (self.act_empty + self.act_occ) / 2.0
            if self.act_occ > self.act_empty else float("inf")
        )

        self.quiet: deque = deque(maxlen=20000)
        self.active: deque = deque(maxlen=20000)
        self.collected: list[np.ndarray] = []
        self._next_report = None

    # ------------------------------------------------------------------ API

    def update(self, now: float, feat, room_scores: dict) -> list[DriftAlert]:
        if not self.enabled or feat is None or not room_scores:
            return []
        if self._next_report is None:
            self._next_report = now + self.report_every_s

        act = self.clf.activity(feat)
        vals = sorted(room_scores.values(), reverse=True)

        if act <= self.act_empty:
            # Logement calme : les scores devraient être à 0.
            self.quiet.append(max(abs(v) for v in vals))
            if self.auto_collect and len(self.collected) < self.max_auto:
                self.collected.append(np.asarray(feat, dtype=np.float64))
        elif act >= self.act_gate:
            # Quelque chose bouge : l'attribution doit être nette, et surtout
            # l'observation doit encore ressembler à une situation calibrée.
            margin = vals[0] - (vals[1] if len(vals) > 1 else 0.0)
            self.active.append((vals[0], margin, self.clf.novelty(feat)))

        if now < self._next_report:
            return []
        self._next_report = now + self.report_every_s
        return self._report()

    def take_collected(self) -> np.ndarray:
        """Récupère et vide les échantillons « vide » accumulés."""
        out = np.asarray(self.collected, dtype=np.float64) if self.collected else None
        self.collected = []
        return out

    # -------------------------------------------------------------- interne

    def _report(self) -> list[DriftAlert]:
        alerts: list[DriftAlert] = []

        if len(self.quiet) >= self.min_samples:
            d = float(np.median(self.quiet))
            if d >= self.recal_db:
                alerts.append(DriftAlert(
                    "baseline", "recalibrate",
                    f"À vide les scores devraient être ~0 dB, ils sont à {d:.1f} dB. "
                    f"Vérifie que tous les nœuds émettent (duty) avant de recalibrer.",
                    d, len(self.quiet)))
            elif d >= self.warn_db:
                alerts.append(DriftAlert(
                    "baseline", "warn",
                    f"Dérive de référence : {d:.1f} dB à vide.", d, len(self.quiet)))

        if len(self.active) >= self.min_samples:
            peaks = np.asarray([p for p, _, _ in self.active])
            margins = np.asarray([m for _, m, _ in self.active])
            novs = np.asarray([n for _, _, n in self.active])
            n = len(self.active)

            # Mesure principale : l'observation ressemble-t-elle encore à une
            # situation calibrée ? C'est la seule qui attrape un logement
            # réaménagé, parce qu'elle ne consulte aucun discriminant (cf. la
            # docstring de RoomClassifier.novelty).
            nov = float(np.median(novs))
            ratio = nov / self.nov_ref if self.nov_ref > 0 else 0.0
            if ratio >= self.nov_warn:
                sev = "recalibrate" if ratio >= self.nov_recal else "warn"
                alerts.append(DriftAlert(
                    "attribution", sev,
                    f"Les mesures ne ressemblent plus à aucune situation calibrée "
                    f"(écart {nov:.2f} contre {self.nov_ref:.2f} de référence, "
                    f"×{ratio:.2f}). Quelque chose a bougé : meuble, nœud, box, "
                    f"ou une porte désormais fermée. Recalibre la pièce concernée.",
                    ratio, n))

            peak = float(np.median(peaks))
            drop = self.peak_ref - peak
            if drop >= self.degrade_db:
                sev = "recalibrate" if drop >= 2 * self.degrade_db else "warn"
                alerts.append(DriftAlert(
                    "attribution", sev,
                    f"Le meilleur score de pièce est tombé à {peak:.1f} dB "
                    f"(référence {self.peak_ref:.1f}).", drop, n))

            m = float(np.median(margins))
            if m < 2.0:
                alerts.append(DriftAlert(
                    "attribution", "warn",
                    f"Écart entre les deux meilleures pièces réduit à {m:.1f} dB : "
                    f"elles deviennent indiscernables.", m, n))

        if not self.quiet and not self.active:
            alerts.append(DriftAlert(
                "coverage", "info",
                "Aucune période franchement vide ni franchement active observée ; "
                "impossible de juger la santé du modèle.", 0.0, 0))

        self.quiet.clear()
        self.active.clear()
        for a in alerts:
            (log.warning if a.severity != "info" else log.info)("dérive[%s] %s",
                                                                a.kind, a.message)
        return alerts
