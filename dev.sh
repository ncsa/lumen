#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Syncing dependencies..."
uv sync

echo "Applying migrations..."
uv run flask --app run db upgrade

echo "Starting app..."
uv run lumen
