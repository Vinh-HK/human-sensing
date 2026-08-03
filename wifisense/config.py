"""Chargement de la configuration (YAML) et du fichier de calibrage."""

from __future__ import annotations

import copy
import os
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "source": {
        "type": "fake",  # fake | limesdr | esp32csi
        "slow_rate_hz": 100.0,
        "block_s": 0.5,
        # --- LimeSDR ---
        "driver": "lime",
        "device_args": "",
        "frequency_hz": 2.437e9,   # canal Wi-Fi 6 ; le LimeSDR plafonne à 3,8 GHz
        "sample_rate_hz": 2.0e6,
        "bandwidth_hz": 3.0e6,
        "gain_db": 40.0,
        "antenna": "LNAH",         # voie haute fréquence, obligatoire à 2,4 GHz
        "fine_decim": 50,
        "gate_db": 6.0,
        "min_gated": 4,
        # --- ESP32 CSI ---
        "udp_host": "0.0.0.0",
        "udp_port": 5566,
        "subcarrier_refresh_s": 20.0,
        "csi_timeout_s": 10.0,
        # --- simulateur ---
        "fake_scenario": "empty",
        "fake_speed": 1.0,
    },
    "traffic": {
        # Le Wi-Fi n'émet que lorsqu'il a quelque chose à dire. Sans trafic,
        # aucune illumination, donc aucune mesure. On en fabrique.
        "enabled": False,
        "target": "",       # IP d'un appareil connecté EN WI-FI (pas en Ethernet)
        "port": 9,
        "rate_hz": 100.0,
        "payload_bytes": 200,
    },
    "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "username": "",
        "password": "",
        "base_topic": "zigbee2mqtt",
        "client_id": "wifisense",
        "refresh_s": 300.0,       # réémission périodique (mailles Zigbee capricieuses)
        "override_s": 900.0,      # durée de respect d'une action manuelle
        "lux_topic": "",          # optionnel : n'allumer que dans le noir
        "lux_field": "illuminance_lux",
        "lux_threshold": 40.0,
    },
    "presence": {
        "on_confirm": 2,
        "hold_s": 120.0,
    },
    "classifier": {
        # Classification par pièce : un classifieur binaire par pièce, sur le
        # vecteur de caractéristiques de TOUS les nœuds. C'est ce qui permet de
        # rejeter les fuites entre pièces voisines, et de gérer plusieurs
        # occupants simultanés.
        "window_s": 8.0,
        "n_bands": 6,
        "lo_hz": 0.2,
        "model_path": "classifier.npz",
        "dataset_path": "calibration_data.npz",
    },
    "drift": {
        # Surveillance de la péremption du modèle : le système signale lui-même
        # qu'il n'est plus fiable, au lieu que ça se découvre trois semaines plus
        # tard parce que la lumière fait n'importe quoi.
        "enabled": True,
        "report_every_s": 3600.0,
        "min_samples": 60,
        "warn_db": 2.5,          # dérive de référence (périodes vides)
        "recalibrate_db": 5.0,
        "degrade_db": 3.0,       # affaissement du meilleur score (périodes actives)
        # Collecte automatique d'échantillons « vide » pendant les périodes
        # calmes. Désactivée par défaut : cf. README, une personne endormie
        # pourrait être apprise comme du vide.
        "auto_collect": False,
        "max_auto_samples": 400,
        "status_topic": "wifisense/drift",
    },
    "decision_log": "",  # chemin d'un journal des décisions (traçabilité)
    "zones": {},
    "log_level": "INFO",
}

ZONE_DEFAULTS: dict[str, Any] = {
    "detector": "shower",
    "channel": 0,        # voie RX (LimeSDR) ou node_id (ESP32)
    "lights": [],
    "threshold_db": 6.0,
    "enabled": True,
    "brightness": None,
    "transition": 1.0,
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# PyYAML implémente YAML 1.1 : « 2.4e9 » y est une chaîne, pas un flottant (la
# grammaire exige « 2.4e+9 »). Plutôt que de compter sur la vigilance de qui
# édite le fichier, on force la conversion des clés numériques connues.
_NUMERIC_KEYS = frozenset(
    {
        "frequency_hz", "sample_rate_hz", "bandwidth_hz", "gain_db",
        "slow_rate_hz", "block_s", "gate_db", "rate_hz", "refresh_s",
        "override_s", "lux_threshold", "hold_s", "threshold_db", "window_s",
        "bg_margin_db", "bg_fall_s", "bg_rise_s", "bg_min_s", "score_floor_db",
        "subcarrier_refresh_s", "csi_timeout_s", "fake_speed", "lo_hz",
        "report_every_s", "warn_db", "recalibrate_db", "degrade_db",
        "novelty_warn_ratio", "novelty_recalibrate_ratio",
        "band_lo_hz", "band_hi_hz", "human_lo_hz", "human_hi_hz",
        "machine_lo_hz", "machine_hi_hz", "work_rate_hz", "stability_tol_hz",
    }
)


def _coerce_numbers(node):
    if isinstance(node, dict):
        return {
            k: (
                float(v)
                if k in _NUMERIC_KEYS and isinstance(v, str) and v.strip()
                else _coerce_numbers(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_coerce_numbers(v) for v in node]
    return node


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        user = yaml.safe_load(fh) or {}
    try:
        user = _coerce_numbers(user)
    except ValueError as exc:
        raise ValueError(f"Valeur numérique invalide dans {path} : {exc}") from exc
    cfg = deep_merge(DEFAULTS, user)

    zones = {}
    for name, z in (cfg.get("zones") or {}).items():
        zones[name] = deep_merge(ZONE_DEFAULTS, z or {})
    cfg["zones"] = zones

    if not zones:
        raise ValueError(f"Aucune zone définie dans {path}")
    for name, z in zones.items():
        if not z.get("lights"):
            raise ValueError(f"Zone {name!r} : liste 'lights' vide")
    return cfg


def load_calibration(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_calibration(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=True, allow_unicode=True)


def baseline_for(calib: dict, zone: str) -> float:
    """Baseline « pièce vide » d'une zone, 0.0 si pas encore calibrée."""
    return float(((calib.get("zones") or {}).get(zone) or {}).get("baseline_db", 0.0))
