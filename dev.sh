#!/bin/bash
set -e

cd "$(dirname "$0")"

# Ensure TimescaleDB is running for local dev
if ! docker ps --format '{{.Names}}' | grep -q '^lumen-tsdb$'; then
  echo "Starting TimescaleDB container..."
  docker pull timescale/timescaledb:latest-pg17
  docker run -d --name lumen-tsdb \
    -e POSTGRES_DB=lumen \
    -e POSTGRES_USER=lumen \
    -e POSTGRES_PASSWORD=lumen \
    -p 5678:5432 \
    timescale/timescaledb:latest-pg17
  echo "Waiting for TimescaleDB to be ready..."
  until docker exec lumen-tsdb pg_isready -U lumen -q; do sleep 1; done
else
  echo "TimescaleDB container already running."
fi

echo "Syncing dependencies..."
uv sync

echo "Applying migrations..."
uv run flask --app run db upgrade

echo "Starting app..."
uv run lumen
