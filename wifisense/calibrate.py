"""Mode calibrage : apprendre le niveau de référence d'une pièce.

Principe : les trois détecteurs rendent un score en dB, mais l'échelle absolue de
ce score dépend de la pièce, du gain du récepteur, de la position de l'antenne et
du niveau de trafic. Un seuil « en dur » ne serait donc transposable nulle part.

On mesure la distribution des scores dans un état connu et on en tire une
référence. Le percentile 95 (et pas le maximum) sert de référence pièce vide :
le maximum est déterminé par le pire artefact de toute la session, ce qui rendrait
le seuil dépendant d'un événement unique.

Le cas important est ``--label machine`` : on enregistre la pièce vide **avec la
machine en marche**. Le détecteur cuisine apprend alors le spectre de la machine
comme fond normal, et la référence intègre son niveau de bruit.
"""

from __future__ import annotations

import logging
import math
import os

import numpy as np

from .engine import Engine

log = logging.getLogger(__name__)

SCALAR_LABELS = ("empty", "machine", "occupied")
LABELS = SCALAR_LABELS


def collect(cfg: dict, calib: dict, duration_s: float, progress=None):
    """Fait tourner la chaîne sans piloter les lampes.

    Rend (scores scalaires par zone, vecteurs de caractéristiques multi-nœuds).
    Les deux sont enregistrés en une seule session : régler les seuils des
    détecteurs et entraîner le classifieur consomment la même mesure, il serait
    absurde de demander deux fois de sortir de chez soi.
    """
    scores: dict[str, list[float]] = {}
    feats: list[np.ndarray] = []

    def on_update(t, zones, diag):
        for z in zones:
            if z.last is not None and math.isfinite(z.last.score_db):
                scores.setdefault(z.name, []).append(z.last.score_db)
        f = diag.get("features")
        if f is not None:
            feats.append(f)
        if progress is not None:
            progress(t, duration_s, zones, diag)

    # baseline forcée à 0 pendant la mesure : on veut le score brut, non décalé.
    flat = dict(calib)
    flat["zones"] = {}
    engine = Engine(cfg, flat, dry_run=True, control_lights=False)
    engine.run(duration_s=duration_s, on_update=on_update)
    return (
        {k: np.asarray(v, dtype=np.float64) for k, v in scores.items()},
        np.asarray(feats, dtype=np.float64) if feats else np.zeros((0, 0)),
    )


# --------------------------------------------------------------- jeu de données


def dataset_load(path: str) -> dict[str, np.ndarray]:
    """Charge le jeu d'entraînement du classifieur (étiquette → vecteurs)."""
    if not path or not os.path.exists(path):
        return {}
    d = np.load(path, allow_pickle=False)
    n = int(d["n_classes"])
    return {str(d[f"label_{i}"]): d[f"data_{i}"] for i in range(n)}


def dataset_save(path: str, data: dict[str, np.ndarray]) -> None:
    # Les étiquettes contiennent des virgules (« cuisine,salon ») : on les range
    # dans des tableaux indexés plutôt que de s'en servir comme noms de clés.
    out = {"n_classes": np.asarray(len(data))}
    for i, (k, v) in enumerate(sorted(data.items())):
        out[f"label_{i}"] = np.asarray(k)
        out[f"data_{i}"] = np.asarray(v, dtype=np.float64)
    np.savez_compressed(path, **out)


def dataset_append(path: str, label: str, feats: np.ndarray, replace: bool = True):
    """Ajoute une session. `replace` écrase la session précédente du même label."""
    data = dataset_load(path)
    if feats.size == 0:
        return data
    if replace or label not in data:
        data[label] = feats
    else:
        data[label] = np.vstack([data[label], feats])
    dataset_save(path, data)
    return data


def summarize(arr: np.ndarray) -> dict:
    return {
        "n": int(arr.size),
        "median_db": float(np.median(arr)),
        "p05_db": float(np.percentile(arr, 5)),
        "p95_db": float(np.percentile(arr, 95)),
        "max_db": float(np.max(arr)),
    }


def merge(calib: dict, label: str, stats: dict[str, dict]) -> dict:
    """Fusionne un relevé dans le fichier de calibrage et recalcule les seuils."""
    out = dict(calib)
    zones = dict(out.get("zones") or {})

    for zone, s in stats.items():
        entry = dict(zones.get(zone) or {})
        entry[label] = s

        # Référence = pire cas « pièce inoccupée ». Si un relevé machine existe,
        # il prime : une machine qui tourne produit toujours plus d'énergie qu'une
        # pièce silencieuse, et c'est ce niveau-là qu'il faut dépasser.
        refs = [entry[k]["p95_db"] for k in ("empty", "machine") if k in entry]
        if refs:
            entry["baseline_db"] = round(max(refs), 2)

        if "occupied" in entry and "baseline_db" in entry:
            occ = entry["occupied"]["p05_db"]
            gap = occ - entry["baseline_db"]
            entry["separation_db"] = round(gap, 2)
            # Seuil au milieu de l'intervalle, plancher à 3 dB pour ne pas
            # descendre dans le bruit de mesure.
            entry["suggested_threshold_db"] = round(max(3.0, gap / 2.0), 2)
        zones[zone] = entry

    out["zones"] = zones
    return out


def report(calib: dict) -> str:
    lines = ["", "Résultat du calibrage", "=" * 60]
    for zone, e in sorted((calib.get("zones") or {}).items()):
        lines.append(f"\n[{zone}]")
        for label in LABELS:
            if label in e:
                s = e[label]
                lines.append(
                    f"  {label:9s} n={s['n']:5d}  médiane={s['median_db']:7.2f} dB"
                    f"  p05={s['p05_db']:7.2f}  p95={s['p95_db']:7.2f}"
                )
        if "baseline_db" in e:
            lines.append(f"  → baseline_db = {e['baseline_db']:.2f}")
        if "separation_db" in e:
            gap = e["separation_db"]
            verdict = (
                "confortable" if gap >= 10 else "juste" if gap >= 5 else "INSUFFISANTE"
            )
            lines.append(
                f"  → séparation vide/occupé = {gap:.2f} dB ({verdict}), "
                f"threshold_db conseillé = {e['suggested_threshold_db']:.2f}"
            )
            if gap < 5:
                lines.append(
                    "     Déplace l'antenne (ou le nœud) : elle doit être en vue"
                    " directe de la zone à surveiller, pas derrière un mur porteur."
                )
    lines.append("")
    return "\n".join(lines)
