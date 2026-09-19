#!/bin/sh
set -eu

SERVICE_ACCOUNT_FILE=""

cleanup() {
    if [ -n "$SERVICE_ACCOUNT_FILE" ] && [ -f "$SERVICE_ACCOUNT_FILE" ]; then
        rm -f "$SERVICE_ACCOUNT_FILE"
    fi
}
trap cleanup EXIT INT TERM

if [ -n "${GEE_SERVICE_ACCOUNT_JSON:-}" ]; then
    SERVICE_ACCOUNT_FILE="/tmp/gee-service-account.json"
    printf '%s' "$GEE_SERVICE_ACCOUNT_JSON" > "$SERVICE_ACCOUNT_FILE"
    chmod 600 "$SERVICE_ACCOUNT_FILE"
    export GEE_SERVICE_ACCOUNT_JSON_PATH="$SERVICE_ACCOUNT_FILE"
fi

export ENABLE_AUTO_LABELING="${ENABLE_AUTO_LABELING:-false}"

exec python gee_fire_context_labeling.py --once
