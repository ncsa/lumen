#!/bin/sh
set -e

if [ "$1" = "bash" ]; then
  exec bash
fi

uv run --no-sync flask --app run db upgrade
exec uv run --no-sync uvicorn asgi:app --host 0.0.0.0 --port 5001 $@
