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

# Serve. Never migrates: the upgrade step (`upgrade` mode, the Compose lumen-upgrade
# service or the Helm migration Job) does that. Wait until the database is at the
# migration head this image expects; the connect timeout covers the gate only.
PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}" flask --app run wait-for-migrations

if [ -n "$PROMETHEUS_MULTIPROC_DIR" ] && [ -d "$PROMETHEUS_MULTIPROC_DIR" ]; then
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db
fi

exec uvicorn asgi:app --host 0.0.0.0 --port 5001 $@
