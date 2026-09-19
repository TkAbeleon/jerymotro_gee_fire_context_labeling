#!/bin/sh
set -eu

SECRET_FILE="/etc/secrets/gee-service-account.json"

if [ ! -f "$SECRET_FILE" ]; then
    echo "ERROR: Render Secret File not found: $SECRET_FILE" >&2
    echo "Create a Secret File named: gee-service-account.json" >&2
    exit 1
fi

export GEE_AUTH_MODE="service_account"
export GEE_SERVICE_ACCOUNT_JSON_PATH="$SECRET_FILE"
export ENABLE_AUTO_LABELING="false"

echo "==> JeryMotro Render Cron Job"
echo "==> GEE service account file: $GEE_SERVICE_ACCOUNT_JSON_PATH"
echo "==> GEE project: ${GEE_PROJECT:-<missing>}"
echo "==> PostgreSQL: configured through DATABASE_URL"

exec python /app/gee_fire_context_labeling.py --once
