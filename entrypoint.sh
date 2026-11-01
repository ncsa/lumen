#!/bin/sh
set -e

if [ "$1" = "bash" ]; then
  exec bash
fi

# One-shot modes. A database host that accepts connections but never answers must not
# hang them: bound every libpq connection attempt unless the operator already did.
if [ "$1" = "upgrade" ]; then
  export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
  exec flask --app run upgrade
fi

if [ "$1" = "wait-for-migrations" ]; then
  shift
  export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
  exec flask --app run wait-for-migrations "$@"
fi

flask --app run db upgrade

if [ -n "$PROMETHEUS_MULTIPROC_DIR" ] && [ -d "$PROMETHEUS_MULTIPROC_DIR" ]; then
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db
fi

exec uvicorn asgi:app --host 0.0.0.0 --port 5001 $@
