#!/bin/sh
set -e

if [ "$1" = "bash" ]; then
  exec bash
fi

flask --app run db upgrade
exec uvicorn asgi:app --host 0.0.0.0 --port 5001 $@
