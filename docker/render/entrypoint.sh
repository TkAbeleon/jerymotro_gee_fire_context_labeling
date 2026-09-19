#!/bin/sh
set -eu
exec uvicorn web_server:app --host 0.0.0.0 --port "${PORT:-10000}"
