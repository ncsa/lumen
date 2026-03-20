#!/bin/sh
set -e

uv run --no-sync flask --app run db upgrade
exec uv run --no-sync uvicorn asgi:app --host 0.0.0.0 --port 5000
