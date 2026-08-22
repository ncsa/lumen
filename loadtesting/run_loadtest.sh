#!/usr/bin/env bash
# Usage: ./loadtesting/run_loadtest.sh [USERS] [MODEL] [CONFIG_YAML]
#   USERS       number of load-test accounts to create (default: 500)
#   MODEL       model name to use (default: dummy)
#   CONFIG_YAML path to Lumen config (default: ./config.yaml)
#
# Starts a full local load-test stack:
#   1. Resets the database
#   2. Starts the dummy LLM backend on :9999
#   3. Starts Lumen with uvicorn (asgi:app, WORKERS processes) on 127.0.0.1:5001
#   4. Creates load-test users and writes their keys to loadtesting/config.yaml
#   5. Verifies the ASGI bridge is actually stamping timing marks
#   6. Opens Locust (web UI at http://localhost:8089)
#
# Concurrency ceiling is WORKERS x WSGI_WORKERS. Both are overridable from the
# shell, e.g. `WORKERS=4 WSGI_WORKERS=50 ./loadtesting/run_loadtest.sh`. Raising
# WSGI_WORKERS above the per-process DB pool capacity just moves the wait from the
# thread pool to pool_timeout -- raise the database's max_connections too.
#
# Ctrl-C stops everything cleanly.
set -eo pipefail

USERS=${1:-500}
MODEL=${2:-dummy}
CONFIG_YAML=${3:-./config.yaml}
LUMEN_HOST=127.0.0.1
LUMEN_PORT=5001
DUMMY_PORT=9999
WORKERS=${WORKERS:-4}
# Threads per process. Consumed by resolve_wsgi_workers() via LUMEN_WSGI_WORKERS;
# "auto" derives it from pool_size + max_overflow, clamped to 10..64.
WSGI_WORKERS=${WSGI_WORKERS:-10}
MONITORING_TOKEN=$(uv run python -c "import yaml; d=yaml.safe_load(open('$CONFIG_YAML')); print(d.get('api',{}).get('monitoring',{}).get('token',''))" 2>/dev/null || echo "")
# DB used by all Lumen invocations below. Default to the Postgres container
# (published on 127.0.0.1:5678 per docker-compose.override.yml). Override with
# `DATABASE_URL=...` at the shell to point elsewhere.
DATABASE_URL=${DATABASE_URL:-postgresql://lumen:lumen@127.0.0.1:5678/lumen}
export DATABASE_URL

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

# ── 2. optional dummy backend ─────────────────────────────────────────────────
# Only the "dummy" model needs the local dummy backend; real models route
# directly to their configured endpoints.
if [ "$MODEL" = "dummy" ]; then
    echo "==> Starting dummy backend on :$DUMMY_PORT..."
    uv run dummy &
    PIDS+=($!)

    until curl -sf -o /dev/null "http://localhost:$DUMMY_PORT/v1/models"; do
        sleep 0.5
    done
    echo "    Dummy backend ready."
else
    echo "    Skipping dummy backend (model: $MODEL)."
fi

# ── 3. Lumen ───────────────────────────────────────────────────────────────────
# Serve asgi:app, NOT run:app --interface wsgi. asgi.py is the only place
# DisconnectAwareWSGIMiddleware is installed, and that bridge is what stamps T0 into
# the environ; without it request_logs.queue_wait/preflight/started_at/send_blocked
# are all NULL, disconnects go undetected, and the thread pool is uvicorn's
# hard-coded 10 per process instead of LUMEN_WSGI_WORKERS. Enforced by
# tests/unit/test_asgi_entrypoint.py.
echo "==> Starting Lumen on $LUMEN_HOST:$LUMEN_PORT ($WORKERS processes x $WSGI_WORKERS threads)..."
CONFIG_YAML="$CONFIG_YAML" \
WEB_CONCURRENCY="$WORKERS" \
LUMEN_WSGI_WORKERS="$WSGI_WORKERS" \
LUMEN_REQUIRE_BRIDGE=1 \
    uv run uvicorn asgi:app \
    --host "$LUMEN_HOST" --port "$LUMEN_PORT" \
    --workers "$WORKERS" \
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

# ── 5. verify the ASGI bridge ──────────────────────────────────────────────────
# Send one real request and check that the bridge stamped its timing marks. A
# 500-user run that produces NULL queue_wait is wasted, and the only visible
# symptom is columns that are empty afterwards -- so assert it up front, cheaply.
echo "==> Verifying the ASGI bridge is stamping timing marks..."
PROBE_KEY=$(uv run python -c "import yaml; print(yaml.safe_load(open('loadtesting/config.yaml'))['api_keys'][0])")
# Retry rather than fail on the first attempt: get_next_endpoint() only returns
# endpoints marked healthy, and the health pass runs on a 60 s interval, so a
# freshly-synced model can be legitimately unusable for up to a minute after boot.
# Aborting the run for that would be a false alarm.
PROBE_BODY=$(mktemp)
PROBE_OK=""
for _ in $(seq 1 30); do
    PROBE_CODE=$(curl -s -o "$PROBE_BODY" -w "%{http_code}" -X POST \
        -H "Authorization: Bearer $PROBE_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":false}" \
        "http://$LUMEN_HOST:$LUMEN_PORT/v1/chat/completions" || echo "000")
    if [ "$PROBE_CODE" = "200" ]; then
        PROBE_OK=1
        break
    fi
    if [ "$PROBE_CODE" = "500" ]; then
        echo "ERROR: probe returned 500. With LUMEN_REQUIRE_BRIDGE=1 that is what a missing" >&2
        echo "       ASGI bridge returns — serve 'asgi:app', never 'run:app --interface wsgi'." >&2
        head -c 400 "$PROBE_BODY" >&2; echo "" >&2
        rm -f "$PROBE_BODY"
        exit 1
    fi
    sleep 3
done
if [ -z "$PROBE_OK" ]; then
    echo "ERROR: probe to model '$MODEL' never succeeded (last HTTP $PROBE_CODE)." >&2
    echo "       Most likely no endpoint for it is marked healthy yet. Check that the" >&2
    echo "       backend is reachable and that model_endpoints.healthy is true." >&2
    head -c 400 "$PROBE_BODY" >&2; echo "" >&2
    rm -f "$PROBE_BODY"
    exit 1
fi
rm -f "$PROBE_BODY"

PROBE_QUEUE_WAIT=$(uv run python - <<'EOF'
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["DATABASE_URL"], poolclass=sa.pool.NullPool)
with e.connect() as c:
    row = c.execute(sa.text(
        "SELECT queue_wait FROM request_logs ORDER BY time DESC LIMIT 1"
    )).scalar()
print("NULL" if row is None else row)
EOF
)
if [ "$PROBE_QUEUE_WAIT" = "NULL" ]; then
    echo "ERROR: request_logs.queue_wait is NULL — the ASGI bridge is not in the request path." >&2
    echo "       Serve 'asgi:app' (never 'run:app --interface wsgi'). See asgi.py." >&2
    exit 1
fi
echo "    Bridge active (queue_wait=$PROBE_QUEUE_WAIT s)."

# ── 6. locust ──────────────────────────────────────────────────────────────────
echo "==> Starting Locust — web UI at http://localhost:8089"
uv run locust
