#!/bin/sh
set -eu

# Le worker GEE utilise ce fichier Render Secret File après le démarrage
# du serveur HTTP. Ne jamais mettre le JSON du Service Account dans Git.

SECRET_FILE="/etc/secrets/gee-service-account.json"

if [ -f "$SECRET_FILE" ]; then
    export GEE_SERVICE_ACCOUNT_JSON_PATH="$SECRET_FILE"
fi

export GEE_AUTH_MODE="${GEE_AUTH_MODE:-service_account}"
export ENABLE_AUTO_LABELING="${ENABLE_AUTO_LABELING:-false}"

# IMPORTANT : ne jamais lancer gee_fire_context_labeling.py directement ici.
# Render doit obtenir immédiatement un serveur HTTP pour passer le health check.
exec uvicorn web_server:app --host 0.0.0.0 --port "${PORT:-10000}"
