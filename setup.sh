#!/usr/bin/env bash
#
# setup.sh — JeryMotro / Enrichissement GEE des détections FIRMS
# -----------------------------------------------------------------
# Ce script :
#   1. Crée l'environnement virtuel Python s'il n'existe pas déjà.
#   2. L'active.
#   3. Installe/actualise les dépendances depuis requirements.txt.
#   4. Vérifie la présence du fichier .env (avertit sinon).
#   5. Lance le script Python principal en lui transmettant tous les
#      arguments reçus par ce script (ex: --once).
#
# Usage :
#   ./setup.sh              # lance en mode normal (respecte ENABLE_AUTO_LABELING)
#   ./setup.sh --once       # force une exécution unique
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="venv"
PYTHON_BIN="python3"
MAIN_SCRIPT="gee_fire_context_labeling.py"
REQUIREMENTS_FILE="requirements.txt"

echo "==> [1/4] Vérification de l'environnement virtuel..."
if [ ! -d "$VENV_DIR" ]; then
    echo "    Environnement virtuel introuvable, création en cours ($VENV_DIR)..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    echo "    Environnement virtuel déjà présent."
fi

echo "==> [2/4] Activation de l'environnement virtuel..."
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> [3/4] Installation des dépendances..."
pip install --upgrade pip --quiet
if [ -f "$REQUIREMENTS_FILE" ]; then
    pip install -r "$REQUIREMENTS_FILE"
else
    echo "    ERREUR : fichier $REQUIREMENTS_FILE introuvable." >&2
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "    ATTENTION : fichier .env introuvable. Le script utilisera uniquement"
    echo "    les variables d'environnement déjà exportées dans ce shell."
fi

echo "==> [4/4] Lancement du script principal ($MAIN_SCRIPT)..."
exec "$PYTHON_BIN" "$MAIN_SCRIPT" "$@"
