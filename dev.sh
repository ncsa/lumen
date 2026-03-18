#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Syncing dependencies..."
uv sync

echo "Applying migrations..."
uv run flask db upgrade

echo "Starting app..."
uv run flask run --debug
