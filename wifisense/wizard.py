"""Assistant d'installation : génère config.yaml par questions-réponses.

Le format de configuration accepte déjà un nombre quelconque de pièces et de
nœuds ; ce qui manquait, c'est de ne pas avoir à l'écrire à la main. L'assistant
pose les questions dans l'ordre où elles se posent physiquement (quel matériel,
combien de pièces, quelles lampes) et produit un fichier commenté.

Il déduit aussi ce qui n'a qu'une bonne réponse : à partir de deux nœuds le mode
classifieur est le seul qui sache rejeter les fuites entre pièces voisines et
gérer plusieurs occupants, donc il est choisi d'office. À un seul nœud, il n'y a
pas de vecteur multi-liens à classifier et il faut retomber sur un détecteur
scalaire.
"""

from __future__ import annotations

import os
import sys

import yaml

# Réglages par défaut selon l'usage de la pièce. Une chambre ou un séjour
# demandent une temporisation longue — c'est précisément le défaut des
# détecteurs de mouvement que ce système corrige. Une salle de bain peut être
# plus courte, on n'y stationne pas.
ROOM_PRESETS = {
    "douche": (90, None), "salle_de_bain": (90, None), "bathroom": (90, None),
    "shower": (90, None),
    "cuisine": (120, None), "kitchen": (120, None),
    "salon": (300, 180), "sejour": (300, 180), "living": (300, 180),
    "chambre": (300, 120), "bedroom": (300, 120),
    "bureau": (300, 200), "office": (300, 200),
}
SCALAR_HINT = {
    "douche": "shower", "salle_de_bain": "shower", "bathroom": "shower",
    "shower": "shower",
    "cuisine": "kitchen", "kitchen": "kitchen",
    "salon": "breathing", "sejour": "breathing", "living": "breathing",
    "chambre": "breathing", "bedroom": "breathing",
}


class Prompt:
    """Questions avec valeur par défaut ; retombe sur les défauts hors terminal."""

    def __init__(self, assume_defaults: bool = False):
        # On ne coupe l'interaction que si elle est explicitement refusée. Se
        # baser sur isatty() ferait ignorer une entrée redirigée, ce qui
        # empêcherait tout script d'installation ; l'absence d'entrée est de
        # toute façon rattrapée par EOFError plus bas.
        self.auto = assume_defaults
        if self.auto:
            print("  (mode --defaults : valeurs par défaut retenues)\n")

    def ask(self, question: str, default: str = "") -> str:
        if self.auto:
            return default
        suffix = f" [{default}]" if default else ""
        try:
            answer = input(f"  {question}{suffix} : ").strip()
        except EOFError:
            return default
        return answer or default

    def ask_int(self, question, default, lo=1, hi=64) -> int:
        while True:
            raw = self.ask(question, str(default))
            try:
                v = int(raw)
            except ValueError:
                print(f"    « {raw} » n'est pas un nombre.")
                continue
            if lo <= v <= hi:
                return v
            print(f"    Attendu entre {lo} et {hi}.")

    def ask_choice(self, question, choices, default) -> str:
        while True:
            v = self.ask(f"{question} ({'/'.join(choices)})", default).lower()
            if v in choices:
                return v
            print(f"    Réponse attendue : {', '.join(choices)}")

    def ask_bool(self, question, default=False) -> bool:
        return self.ask_choice(question, ["o", "n"], "o" if default else "n") == "o"


def _slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in name.strip().lower())
    for a, b in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("ç", "c")):
        out = out.replace(a, b)
    return "_".join(p for p in out.split("_") if p) or "piece"


def build_config(p: Prompt) -> tuple[dict, list[str]]:
    print("\n" + "=" * 66)
    print("  Assistant d'installation wifisense")
    print("=" * 66 + "\n")

    # ------------------------------------------------------------- matériel
    print("── Matériel ──")
    src = p.ask_choice(
        "Type de capteur", ["esp32csi", "limesdr", "fake"], "esp32csi")
    source = {"type": src, "block_s": 0.5}

    if src == "esp32csi":
        source["slow_rate_hz"] = 20.0
        source["udp_port"] = p.ask_int("Port UDP d'écoute du CSI", 5566, 1, 65535)
    elif src == "limesdr":
        source["slow_rate_hz"] = 100.0
        ch = p.ask_choice("Canal Wi-Fi à écouter", ["1", "6", "11"], "6")
        source["frequency_hz"] = {"1": 2412, "6": 2437, "11": 2462}[ch] * 1000000
        source["sample_rate_hz"] = 2000000
        source["fine_decim"] = 50
        source["gain_db"] = float(p.ask_int("Gain RX en dB", 40, 0, 73))
        source["antenna"] = "LNAH"
        print("    Rappel : le LimeSDR plafonne à 3,5–3,8 GHz. Le Wi-Fi 5 GHz est")
        print("    hors de portée, il faut un SSID 2,4 GHz actif.\n")
    else:
        source["slow_rate_hz"] = 100.0
        source["sample_rate_hz"] = 200000
        source["fine_decim"] = 10
        source["fake_scenario"] = "empty"

    # ---------------------------------------------------------------- zones
    print("\n── Pièces ──")
    if src == "limesdr":
        print("    Un SDR = un point de mesure. La LimeSDR Mini n'a qu'UNE voie RX ;")
        print("    la LimeSDR-USB en a deux. Au-delà, il faut des nœuds ESP32.\n")
    n = p.ask_int("Combien de pièces à surveiller", 4, 1, 16)

    zones, names = {}, []
    for i in range(n):
        raw = p.ask(f"Nom de la pièce {i + 1}", "")
        name = _slug(raw) if raw else f"piece_{i + 1}"
        names.append(name)
        hold, bright = ROOM_PRESETS.get(name, (120, None))

        lights = p.ask(
            f"    Lampes Zigbee2MQTT de « {name} » (noms séparés par des virgules)",
            f"lampe_{name}")
        z = {
            "channel": i,
            "lights": [s.strip() for s in lights.split(",") if s.strip()],
            "hold_s": p.ask_int(f"    Délai avant extinction de « {name} » (s)",
                                hold, 10, 3600),
        }
        if bright is not None:
            z["brightness"] = bright
        zones[name] = z

    # Le mode de détection découle du nombre de nœuds, pas d'une préférence.
    if n >= 2:
        print(f"\n  → {n} nœuds : mode classifieur (rejet des fuites entre pièces,")
        print("    gestion de plusieurs occupants). Seuil par défaut 4 dB.")
        for z in zones.values():
            z["detector"] = "classifier"
            z["threshold_db"] = 4.0
    else:
        only = names[0]
        print("\n  → Un seul nœud : pas de vecteur multi-liens à classifier,")
        print("    on utilise un détecteur scalaire.")
        det = p.ask_choice("    Détecteur", ["shower", "breathing", "kitchen"],
                           SCALAR_HINT.get(only, "shower"))
        zones[only]["detector"] = det
        zones[only]["threshold_db"] = 8.0 if det == "breathing" else 6.0

    # ---------------------------------------------------------------- MQTT
    print("\n── Zigbee2MQTT ──")
    mqtt = {
        "host": p.ask("Adresse du broker MQTT", "127.0.0.1"),
        "port": p.ask_int("Port", 1883, 1, 65535),
        "base_topic": p.ask("Topic de base Zigbee2MQTT", "zigbee2mqtt"),
    }
    if p.ask_bool("Le broker demande-t-il une authentification", False):
        mqtt["username"] = p.ask("    Utilisateur", "")
        mqtt["password"] = p.ask("    Mot de passe", "")
    print("    (le système tourne même sans broker : la détection continue et")
    print("     les commandes sont rejouées à la reconnexion)")

    # -------------------------------------------------------------- trafic
    print("\n── Trafic d'illumination ──")
    print("  Le Wi-Fi n'émet que s'il a du trafic. La cible doit être un appareil")
    print("  connecté EN WI-FI — viser la box depuis un serveur câblé ne produit")
    print("  aucune onde, les paquets resteraient sur le cuivre.")
    traffic = {"enabled": False, "port": 9, "rate_hz": 100, "payload_bytes": 200}
    if p.ask_bool("Activer le générateur de trafic", src != "fake"):
        traffic["enabled"] = True
        traffic["target"] = p.ask("    IP de l'appareil Wi-Fi cible", "192.168.1.1")

    cfg = {
        "log_level": "INFO",
        "source": source,
        "traffic": traffic,
        "mqtt": mqtt,
        "presence": {"on_confirm": 2, "hold_s": 120},
        "classifier": {
            "window_s": 8.0, "n_bands": 6,
            "model_path": "classifier.npz",
            "dataset_path": "calibration_data.npz",
        },
        "drift": {"enabled": True, "status_topic": "wifisense/drift"},
        "decision_log": "decisions.jsonl",
        "zones": zones,
    }
    return cfg, names


HEADER = """\
# Configuration wifisense — générée par « wifisense setup ».
#
# Nombres en Hz écrits en entier volontairement : PyYAML suit YAML 1.1, où
# « 2.4e9 » est lu comme une CHAÎNE et non un flottant.
#
# Documentation complète : README.md
"""


def plan(cfg: dict, names: list[str]) -> list[str]:
    """Plan de calibrage adapté aux pièces déclarées."""
    multi = any(z.get("detector") == "classifier" for z in cfg["zones"].values())
    steps = ["python -m wifisense calibrate --label empty --duration 300"]
    if multi:
        steps += [
            f"python -m wifisense calibrate --label {r} --duration 300" for r in names
        ]
        if len(names) >= 2:
            steps.append(
                f"python -m wifisense calibrate --label {names[0]},{names[1]} "
                f"--duration 300    # session à deux occupants"
            )
        steps.append("python -m wifisense fit")
    else:
        steps.append(
            "python -m wifisense calibrate --label machine --duration 300"
            "   # si une machine est dans la pièce"
        )
        steps.append(
            f"python -m wifisense calibrate --label {names[0]} --duration 300"
        )
    return steps


def write(cfg: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HEADER)
        yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True,
                       default_flow_style=False)


def summary(cfg: dict, names: list[str], path: str) -> str:
    lines = ["", "=" * 66, f"  Configuration écrite dans {path}", "=" * 66, ""]
    lines.append(f"  Source    : {cfg['source']['type']} "
                 f"({cfg['source']['slow_rate_hz']:.0f} Hz)")
    lines.append(f"  Pièces    : {len(cfg['zones'])}")
    for name, z in cfg["zones"].items():
        lines.append(f"    · {name:<14} voie {z['channel']}  "
                     f"{z['detector']:<10} extinction {z['hold_s']}s  "
                     f"→ {', '.join(z['lights'])}")
    lines += ["", "  Étapes suivantes", "  " + "-" * 62, ""]
    lines.append("  1. Vérifier que tout arrive :")
    lines.append("       python -m wifisense doctor")
    lines.append("")
    lines.append("  2. Vérifier le placement AVANT de calibrer "
                 "(regarde la colonne duty) :")
    lines.append("       python -m wifisense monitor")
    lines.append("")
    lines.append("  3. Calibrer — sortez tous du logement pour « empty » :")
    for s in plan(cfg, names):
        lines.append(f"       {s}")
    lines.append("")
    lines.append("  4. Lancer :")
    lines.append("       python -m wifisense run")
    lines.append("")
    return "\n".join(lines)


def run(path: str, assume_defaults: bool = False, force: bool = False) -> int:
    if os.path.exists(path) and not force:
        print(f"{path} existe déjà. Utilise --force pour l'écraser.", file=sys.stderr)
        return 1
    p = Prompt(assume_defaults)
    cfg, names = build_config(p)
    write(cfg, path)
    print(summary(cfg, names, path))
    return 0
