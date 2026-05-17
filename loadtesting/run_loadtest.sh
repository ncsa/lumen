#!/usr/bin/env bash
# Usage: ./loadtesting/run_loadtest.sh [USERS] [MODEL] [CONFIG_YAML]
#   USERS       number of load-test accounts to create (default: 500)
#   MODEL       model name to use (default: dummy)
#   CONFIG_YAML path to Lumen config (default: ./config.yaml)
#
# Starts a full local load-test stack:
#   1. Resets the database
#   2. Starts the dummy LLM backend on :9999
#   3. Starts Lumen with uvicorn (4 workers) on 127.0.0.1:5000
#   4. Creates load-test users and writes their keys to loadtesting/config.yaml
#   5. Opens Locust (web UI at http://localhost:8089)
#
# Ctrl-C stops everything cleanly.
set -eo pipefail

USERS=${1:-500}
MODEL=${2:-dummy}
CONFIG_YAML=${3:-./config.yaml}
LUMEN_HOST=127.0.0.1
LUMEN_PORT=5001
DUMMY_PORT=9999
WORKERS=4
MONITORING_TOKEN=$(uv run python -c "import yaml; d=yaml.safe_load(open('$CONFIG_YAML')); print(d.get('monitoring',{}).get('token',''))" 2>/dev/null || echo "")

# ── cleanup ────────────────────────────────────────────────────────────────────
PIDS=()
cleanup() {
    echo ""
    echo "==> Shutting down..."
    for pid in "${PIDS[@]+"${PIDS[@]}"}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

# ── 1. reset DB ────────────────────────────────────────────────────────────────
echo "==> Resetting database..."
CONFIG_YAML="$CONFIG_YAML" uv run python -c "
from run import app
from lumen.extensions import db
from sqlalchemy import text
with app.app_context():
    with db.engine.begin() as conn:
        conn.execute(text('DELETE FROM entities'))
    print('    Entities cleared (cascaded to api_keys, balances, limits, access, logs, stats).')
"
echo "    Database ready."

# ── 2. dummy backend ───────────────────────────────────────────────────────────
echo "==> Starting dummy backend on :$DUMMY_PORT..."
uv run dummy &
PIDS+=($!)

until curl -sf -o /dev/null "http://localhost:$DUMMY_PORT/v1/models"; do
    sleep 0.5
done
echo "    Dummy backend ready."

# ── 3. Lumen ───────────────────────────────────────────────────────────────────
echo "==> Starting Lumen on $LUMEN_HOST:$LUMEN_PORT ($WORKERS workers)..."
CONFIG_YAML="$CONFIG_YAML" uv run uvicorn run:app \
    --host "$LUMEN_HOST" --port "$LUMEN_PORT" \
    --workers "$WORKERS" --interface wsgi \
    --log-level warning &
PIDS+=($!)

AUTH_HEADER=""
if [ -n "$MONITORING_TOKEN" ]; then
    AUTH_HEADER="Authorization: Bearer $MONITORING_TOKEN"
fi

until curl -sf -o /dev/null \
    ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
    "http://$LUMEN_HOST:$LUMEN_PORT/v1/models"; do
    sleep 1
done
echo "    Lumen ready."

# ── 4. create users ────────────────────────────────────────────────────────────
echo "==> Creating $USERS load-test users..."
CONFIG_YAML="$CONFIG_YAML" uv run python loadtesting/setup_users.py \
    "$USERS" --model "$MODEL" --write-config
echo "    Users created."

# ensure base_url points to 127.0.0.1 (macOS AirPlay occupies localhost:5000)
uv run python - <<EOF
import yaml, pathlib
p = pathlib.Path("loadtesting/config.yaml")
cfg = yaml.safe_load(p.read_text())
cfg["base_url"] = "http://$LUMEN_HOST:$LUMEN_PORT"
p.write_text(yaml.dump(cfg, default_flow_style=False))
print(f"    base_url set to http://$LUMEN_HOST:$LUMEN_PORT")
EOF

# ── 5. locust ──────────────────────────────────────────────────────────────────
echo "==> Starting Locust — web UI at http://localhost:8089"
uv run locust
