#!/bin/sh
set -e

if [ "$1" = "bash" ]; then
  exec bash
fi

flask --app run db upgrade

if [ -n "$PROMETHEUS_MULTIPROC_DIR" ] && [ -d "$PROMETHEUS_MULTIPROC_DIR" ]; then
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db
fi

exec uvicorn asgi:app --host 0.0.0.0 --port 5001 $@
