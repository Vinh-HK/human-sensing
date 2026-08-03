#!/usr/bin/env bash
# Installation des dépendances pour wifisense (Debian / Ubuntu).
#
#   ./install.sh            → dépendances Python seulement (mode simulateur)
#   ./install.sh --sdr      → + LimeSuite / SoapySDR pour le LimeSDR
#
# Le paquet SoapySDR n'existe pas sur PyPI : il vient de python3-soapysdr, d'où
# le venv créé avec --system-site-packages.

set -euo pipefail

WITH_SDR=0
[[ "${1:-}" == "--sdr" ]] && WITH_SDR=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/venv"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m/!\\\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERREUR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v apt-get >/dev/null || die "Ce script cible Debian/Ubuntu (apt-get absent)."
[[ $EUID -eq 0 ]] && SUDO="" || SUDO="sudo"

log "Paquets système de base"
$SUDO apt-get update -qq
$SUDO apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev build-essential ca-certificates

if [[ $WITH_SDR -eq 1 ]]; then
    log "LimeSuite + SoapySDR"
    # Sur Ubuntu >= 22.04 et Debian >= 12 ces paquets sont dans les dépôts
    # officiels (universe pour Ubuntu). Inutile d'ajouter la PPA Myriad-RF, qui
    # n'est de toute façon plus maintenue pour les versions récentes.
    $SUDO apt-get install -y --no-install-recommends \
        limesuite liblimesuite-dev soapysdr-tools \
        soapysdr-module-lms7 python3-soapysdr \
      || die "Installation SDR échouée. Vérifie que le dépôt 'universe' est activé :
       sudo add-apt-repository universe && sudo apt-get update"

    # Les règles udev du paquet donnent l'accès au LimeSDR sans être root.
    if [[ ! -e /etc/udev/rules.d/64-limesuite.rules ]] \
       && [[ -e /usr/lib/udev/rules.d/64-limesuite.rules ]]; then
        log "Règles udev"
        $SUDO udevadm control --reload-rules && $SUDO udevadm trigger
    fi
fi

log "Environnement Python ($VENV)"
if [[ ! -d "$VENV" ]]; then
    # --system-site-packages est indispensable : c'est le seul moyen de voir
    # le module SoapySDR installé par apt depuis le venv.
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$HERE/requirements.txt"

log "Vérifications"
"$VENV/bin/python" - <<'PY'
import importlib, sys
ok = True
for mod, why in [("numpy", "obligatoire"), ("scipy", "obligatoire"),
                 ("yaml", "obligatoire"), ("paho.mqtt", "pilotage Zigbee2MQTT")]:
    try:
        importlib.import_module(mod)
        print(f"  [OK ] {mod}")
    except ImportError:
        ok = False
        print(f"  [KO ] {mod} ({why})")
try:
    import SoapySDR
    print(f"  [OK ] SoapySDR {SoapySDR.getAPIVersion()}")
    devs = SoapySDR.Device.enumerate()
    if devs:
        for d in devs:
            print(f"         périphérique détecté : {dict(d)}")
    else:
        print("         aucun SDR détecté (normal si rien n'est branché)")
except ImportError:
    print("  [ -- ] SoapySDR absent — mode simulateur / ESP32 uniquement")
sys.exit(0 if ok else 1)
PY

if [[ $WITH_SDR -eq 1 ]] && command -v LimeUtil >/dev/null; then
    log "LimeUtil --find"
    LimeUtil --find || warn "Aucun LimeSDR trouvé. Branche-le en USB 3.0 (port bleu)."
    warn "Si le firmware est ancien : sudo LimeUtil --update"
fi

cat <<EOF

Installation terminée.

  1. Valider la chaîne sans matériel :
       $VENV/bin/python -m wifisense selftest

  2. Créer la configuration :
       cp config.example.yaml config.yaml && \$EDITOR config.yaml

  3. Calibrer la ou les pièces (sortir de la pièce !) :
       $VENV/bin/python -m wifisense calibrate --label empty --duration 300

  4. Observer en direct avant de brancher les lampes :
       $VENV/bin/python -m wifisense monitor

  5. Lancer :
       $VENV/bin/python -m wifisense run

EOF
