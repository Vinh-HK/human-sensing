"""Classification par pièce : un classifieur binaire indépendant par pièce.

Pourquoi pas un classifieur unique « dans quelle pièce est la personne ? » :
parce qu'il y a plusieurs occupants. Une étiquette unique suppose une seule
personne, et la combinaison « quelqu'un en cuisine ET quelqu'un au salon » ne
figure dans aucun jeu d'apprentissage raisonnable — il y en aurait 2^n.

On pose donc n questions binaires indépendantes, chacune sur le vecteur
multi-nœuds **complet**. C'est ce dernier point qui fait la différence avec de
simples seuils par nœud : en apprenant les sessions « quelqu'un au salon » comme
exemples *négatifs* pour la cuisine, le classifieur de la cuisine apprend à quoi
ressemble une fuite venant du salon, et à la rejeter. Un seuil local n'a aucun
moyen de faire cette distinction.

Calibrage requis : une session par pièce en occupation simple, plus une session
appartement vide. Pas les 2^n combinaisons — les perturbations s'additionnent
approximativement, donc l'apprentissage en occupation simple généralise.

Méthode : discriminant linéaire de Fisher avec covariance régularisée. Pas de
réseau de neurones, et c'est délibéré — avec quelques centaines d'exemples pour
24 dimensions, un modèle linéaire régularisé généralise mieux d'un jour à
l'autre qu'un réseau qui apprendra la session d'enregistrement.
"""

from __future__ import annotations

import json
import logging

import numpy as np

log = logging.getLogger(__name__)

EMPTY = "empty"


def parse_label(label: str) -> frozenset[str]:
    """« empty » → {} ; « cuisine » → {cuisine} ; « cuisine,salon » → les deux.

    Les étiquettes multiples permettent d'enregistrer une session à deux
    occupants sans avoir à couvrir les 2^n combinaisons : il suffit d'ajouter
    celles qui arrivent réellement chez toi.
    """
    label = (label or "").strip()
    if not label or label == EMPTY:
        return frozenset()
    return frozenset(p.strip() for p in label.split(",") if p.strip())


def canonical(rooms) -> str:
    return ",".join(sorted(rooms)) if rooms else EMPTY


class ClassifierZone:
    """Adaptateur : fait passer une zone pilotée par le classifieur pour un
    détecteur ordinaire, afin que `monitor`, le calibrage et la configuration
    restent identiques quel que soit le mode."""

    STREAM = None
    name = "classifier"

    def __init__(self, room: str, cfg: dict, baseline_db: float = 0.0):
        self.room = room
        self.baseline_db = float(baseline_db)
        # Seuil plus bas qu'un détecteur scalaire : sous occupation multiple les
        # scores se rabotent mutuellement (les poids négatifs qui rejettent les
        # fuites du voisinage retranchent aussi l'activité réelle du voisin).
        self.threshold_db = float(cfg.get("threshold_db", 4.0))


class RoomClassifier:
    def __init__(self, feature_names, rooms, mu, sigma, w, p_neg, p_pos, fisher,
                 z_empty=None, refs=None, centroids=None):
        self.feature_names = list(feature_names)
        self.rooms = list(rooms)
        self.mu = np.asarray(mu, dtype=np.float64)
        self.sigma = np.asarray(sigma, dtype=np.float64)
        self.w = {r: np.asarray(w[r], dtype=np.float64) for r in self.rooms}
        self.p_neg = {r: float(p_neg[r]) for r in self.rooms}
        self.p_pos = {r: float(p_pos[r]) for r in self.rooms}
        self.fisher = {r: float(fisher[r]) for r in self.rooms}
        # Centroïde de la classe vide et références de santé, figés à
        # l'entraînement : c'est à eux que le moniteur de dérive compare l'état
        # courant pour dire « le modèle ne colle plus ».
        self.z_empty = (
            np.zeros_like(self.mu) if z_empty is None
            else np.asarray(z_empty, dtype=np.float64)
        )
        self.refs = dict(refs or {})
        self.centroids = {
            k: np.asarray(v, dtype=np.float64) for k, v in (centroids or {}).items()
        }

    def standardize(self, x) -> np.ndarray:
        return (np.asarray(x, dtype=np.float64) - self.mu) / self.sigma

    def activity(self, x) -> float:
        """Distance à la signature « logement vide ».

        Mesure indépendante du classifieur : elle dit « ça bouge quelque part »
        sans rien présumer de la pièce. Indispensable pour juger la santé de
        l'attribution — sinon on utiliserait le modèle pour s'auto-évaluer.
        """
        return float(np.linalg.norm(self.standardize(x) - self.z_empty))

    def novelty(self, x) -> float:
        """Distance à la situation connue la plus proche.

        C'est la mesure qui détecte un logement réaménagé, et elle est la seule
        à le pouvoir. Après un déplacement de meubles, quelqu'un au salon peut
        se mettre à ressembler à la cuisine : le classifieur cuisine se déclenche
        franchement, son score reste élevé, et toute mesure de *confiance* le
        déclare en bonne santé. Le modèle est confortablement dans l'erreur.

        En revanche, le vecteur observé s'éloigne de **tous** les centroïdes
        appris — il ne ressemble plus à aucune situation calibrée. Cette distance
        ne consulte aucun discriminant, donc elle reste valide même quand les
        discriminants se trompent.
        """
        z = self.standardize(x)
        if not self.centroids:
            return 0.0
        return float(min(np.linalg.norm(z - c) for c in self.centroids.values()))

    # ------------------------------------------------------------------ fit

    @classmethod
    def fit(cls, samples: dict[str, np.ndarray], feature_names,
            reg_alpha: float = 0.15, min_samples: int = 20) -> "RoomClassifier":
        labels = {k: parse_label(k) for k in samples}
        rooms = sorted({r for s in labels.values() for r in s})
        if not rooms:
            raise ValueError("Aucune pièce dans le calibrage (labels 'piece:<nom>')")
        if not any(len(s) == 0 for s in labels.values()):
            raise ValueError(
                "Session 'empty' manquante. C'est la mesure la plus importante : "
                "lance `calibrate --label empty` en étant tous sortis."
            )

        for name, arr in samples.items():
            if len(arr) < min_samples:
                log.warning(
                    "Classe %r : seulement %d exemples (%d recommandés). "
                    "Rallonge la session de calibrage.",
                    name, len(arr), min_samples,
                )

        allx = np.vstack([np.asarray(v, dtype=np.float64) for v in samples.values()])
        mu = allx.mean(axis=0)
        sigma = allx.std(axis=0)
        sigma[sigma < 1e-6] = 1.0  # caractéristique constante : neutralisée

        def z(a):
            return (np.asarray(a, dtype=np.float64) - mu) / sigma

        d = allx.shape[1]
        w, p_neg, p_pos, fisher = {}, {}, {}, {}

        for room in rooms:
            # Positifs : toute session où cette pièce est occupée, seule ou non.
            # Négatifs : toute session où elle ne l'est pas — y compris les
            # sessions d'autres pièces, ce qui est exactement ce qui apprend au
            # classifieur à rejeter les fuites du voisinage.
            pk = [k for k in samples if room in labels[k]]
            nk = [k for k in samples if room not in labels[k]]
            if not pk or not nk:
                raise ValueError(
                    f"Pièce {room!r} : il faut au moins une session avec et une "
                    f"session sans (trouvé {len(pk)} / {len(nk)})"
                )
            pos = np.vstack([z(samples[k]) for k in pk])
            neg = np.vstack([z(samples[k]) for k in nk])

            m_pos, m_neg = pos.mean(axis=0), neg.mean(axis=0)
            c_pos = np.cov(pos, rowvar=False) if len(pos) > 1 else np.eye(d)
            c_neg = np.cov(neg, rowvar=False) if len(neg) > 1 else np.eye(d)
            n_p, n_n = len(pos), len(neg)
            sw = (c_pos * n_p + c_neg * n_n) / (n_p + n_n)
            sw = np.atleast_2d(sw)

            # Régularisation indispensable : avec d≈24 et quelques centaines
            # d'exemples, la covariance empirique est mal conditionnée et la
            # direction obtenue serait dominée par le bruit d'estimation.
            sw += np.eye(d) * (reg_alpha * np.trace(sw) / d + 1e-9)

            delta = m_pos - m_neg
            wi = np.linalg.solve(sw, delta)
            nrm = np.linalg.norm(wi)
            if nrm < 1e-12:
                log.warning("Pièce %r : classes indiscernables, direction nulle", room)
                wi = delta
                nrm = max(np.linalg.norm(wi), 1e-12)
            wi = wi / nrm

            a, b = float(wi @ m_neg), float(wi @ m_pos)
            if b <= a:  # projection inversée : on la retourne
                wi, a, b = -wi, -a, -b
            spread = float(np.sqrt(max(wi @ sw @ wi, 1e-12)))

            w[room], p_neg[room], p_pos[room] = wi, a, b
            fisher[room] = (b - a) / spread

        empty_keys = [k for k, s in labels.items() if not s]
        z_empty = np.vstack([z(samples[k]) for k in empty_keys]).mean(axis=0)
        centroids = {k: z(samples[k]).mean(axis=0) for k in samples}

        clf = cls(feature_names, rooms, mu, sigma, w, p_neg, p_pos, fisher,
                  z_empty, None, centroids)

        # Références de santé mesurées sur les données d'entraînement elles-mêmes.
        act_empty = [clf.activity(v) for k in empty_keys for v in samples[k]]
        occ_keys = [k for k, s in labels.items() if s]
        act_occ, peaks, nov = [], [], []
        for k in occ_keys:
            for v in samples[k]:
                act_occ.append(clf.activity(v))
                peaks.append(max(clf.scores(v).values()))
                nov.append(clf.novelty(v))
        clf.refs = {
            "activity_empty_p95": float(np.percentile(act_empty, 95)),
            "activity_occupied_p50": float(np.median(act_occ)),
            "peak_score_p50": float(np.median(peaks)),
            "peak_score_p10": float(np.percentile(peaks, 10)),
            "novelty_p50": float(np.median(nov)),
            "novelty_p95": float(np.percentile(nov, 95)),
        }
        return clf

    # ---------------------------------------------------------------- score

    def scores(self, x: np.ndarray) -> dict[str, float]:
        """Score par pièce, en dB : 0 = signature « absente », 10 = « présente ».

        L'échelle est volontairement calquée sur celle des détecteurs
        (`score > baseline + threshold_db`) pour que le calibrage, l'affichage et
        la configuration restent identiques quel que soit le mode.
        """
        z = (np.asarray(x, dtype=np.float64) - self.mu) / self.sigma
        out = {}
        for room in self.rooms:
            p = float(self.w[room] @ z)
            span = self.p_pos[room] - self.p_neg[room]
            out[room] = 10.0 * (p - self.p_neg[room]) / (span if span > 1e-12 else 1.0)
        return out

    # ------------------------------------------------------------ (de)serial

    def save(self, path: str) -> None:
        np.savez(
            path,
            meta=np.asarray(json.dumps({
                "feature_names": self.feature_names,
                "rooms": self.rooms,
                "p_neg": self.p_neg,
                "p_pos": self.p_pos,
                "fisher": self.fisher,
                "refs": self.refs,
            })),
            classes=np.asarray(sorted(self.centroids)),
            mu=self.mu,
            sigma=self.sigma,
            z_empty=self.z_empty,
            **{f"w__{r}": self.w[r] for r in self.rooms},
            **{f"c__{i}": self.centroids[k]
               for i, k in enumerate(sorted(self.centroids))},
        )

    @classmethod
    def load(cls, path: str) -> "RoomClassifier":
        data = np.load(path, allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        rooms = meta["rooms"]
        classes = (
            [str(c) for c in data["classes"]] if "classes" in data.files else []
        )
        return cls(
            meta["feature_names"], rooms, data["mu"], data["sigma"],
            {r: data[f"w__{r}"] for r in rooms},
            meta["p_neg"], meta["p_pos"], meta["fisher"],
            data["z_empty"] if "z_empty" in data.files else None,
            meta.get("refs"),
            {k: data[f"c__{i}"] for i, k in enumerate(classes)},
        )

    # ----------------------------------------------------------- diagnostic

    def report(self) -> str:
        lines = ["", "Qualité du classifieur (ratio de Fisher par pièce)", "=" * 58]
        for room in self.rooms:
            f = self.fisher[room]
            verdict = (
                "excellent" if f >= 3 else
                "correct" if f >= 2 else
                "faible" if f >= 1 else
                "INEXPLOITABLE"
            )
            lines.append(f"  {room:<14} {f:6.2f}   {verdict}")
        lines.append("")
        if min(self.fisher.values(), default=0) < 1.0:
            lines.append(
                "  Une pièce sous 1.0 n'est pas séparable des autres. Déplace son\n"
                "  nœud : il doit se trouver sur le trajet que tu empruntes\n"
                "  réellement, pas dans un coin. C'est le placement qui décide,\n"
                "  pas le seuil."
            )
            lines.append("")
        return "\n".join(lines)
