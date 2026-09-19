#!/bin/sh
set -eu

# Render Secret File recommandé :
# /etc/secrets/gee-service-account.json
#
# Le script accepte aussi GEE_SERVICE_ACCOUNT_JSON pour les déploiements
# qui préfèrent fournir le JSON comme variable d'environnement.

SECRET_FILE="/etc/secrets/gee-service-account.json"

if [ -f "$SECRET_FILE" ]; then
    export GEE_SERVICE_ACCOUNT_JSON_PATH="$SECRET_FILE"
elif [ -n "${GEE_SERVICE_ACCOUNT_JSON:-}" ]; then
    TMP_FILE="/tmp/gee-service-account.json"
    printf '%s' "$GEE_SERVICE_ACCOUNT_JSON" > "$TMP_FILE"
    chmod 600 "$TMP_FILE"
    export GEE_SERVICE_ACCOUNT_JSON_PATH="$TMP_FILE"

    cleanup() {
        rm -f "$TMP_FILE"
    }
    trap cleanup EXIT INT TERM
else
    echo "ERREUR: aucun compte de service GEE fourni." >&2
    echo "Ajoutez /etc/secrets/gee-service-account.json dans Render." >&2
    exit 1
fi

export GEE_AUTH_MODE="${GEE_AUTH_MODE:-service_account}"
export ENABLE_AUTO_LABELING="${ENABLE_AUTO_LABELING:-false}"

exec python gee_fire_context_labeling.py --once
