#!/bin/sh
set -e

uv run flask --app run db upgrade
exec uv run uvicorn asgi:app --host 0.0.0.0 --port 5000
