# Lumen Load Testing

Locust-based load testing toolkit for the Lumen API. Uses a dummy backend so you can benchmark the full auth/token/routing stack without hitting real models.

## Setup

**1. Install dependencies** (from the project root — `locust` is a dev dependency):

```bash
uv sync
```

**2. Add the dummy model to the main `config.yaml`** (one-time):

```yaml
models:
  - name: dummy
    active: true
    input_cost_per_million: 0.0
    output_cost_per_million: 0.0
    endpoints:
      - url: http://localhost:9999/v1
        api_key: dummy
        model: dummy
```

Restart Lumen after editing, then mark the dummy endpoint healthy in the admin UI.

**3. Create load-test users** (from the project root):

```bash
# Create 10 users, write their API keys into loadtesting/config.yaml
uv run python loadtesting/setup_users.py 10 --write-config

# Options:
#   --model dummy        model to grant tokens for (default: dummy)
#   --tokens 1000000     tokens per user (default: 1,000,000)
#   --prefix loadtest    entity name prefix (default: loadtest)
#   --write-config       update loadtesting/config.yaml with the new keys
```

**4. Edit `config.yaml`** if needed (base URL, model, prompt):

```yaml
base_url: http://localhost:5000
model: dummy
prompt: "Say hello in one sentence."
api_keys:
  - sk-...
  - sk-...
```

---

## Running Tests

All commands run from the **project root**:

```bash
# Terminal 1 — start the dummy backend
uv run dummy

# Terminal 2 — start Lumen
uv run flask run

# Terminal 3 — web UI (open http://localhost:8089)
uv run locust

# Or headless
uv run locust --headless -u 10 -r 2 --run-time 60s
```

### macOS file descriptor limit

With 500+ concurrent users, macOS's default open-file limit (256) will cause `OSError: [Errno 24] Too many open files` in werkzeug. Raise it before starting Lumen:

```bash
ulimit -n 65536
```

Also prefer uvicorn over the Flask dev server for load testing — it handles concurrent connections far more efficiently:

```bash
# Terminal 2 — start Lumen with uvicorn instead of flask run
ulimit -n 65536 && uv run uvicorn asgi:app --host 0.0.0.0 --port 5000 --workers 4
```

---

### Key flags

| Flag | Description |
|------|-------------|
| `-u N` | Number of concurrent users |
| `-r N` | Users spawned per second |
| `--run-time 60s` | Stop after this duration |
| `--headless` | No web UI, print stats to terminal |
| `--host URL` | Override `base_url` from config |

---

## How It Works

- Each Locust user is assigned a different API key round-robin from `config.yaml`.
- With N keys the effective rate-limit ceiling is N × 30 req/min (Lumen's default per-key limit).
- The dummy backend (`dummy_backend.py`) responds instantly with `"Hello"` and fixed token counts, so measured latency reflects Lumen overhead only.
- Locust tracks RPS, latency percentiles (p50/p95/p99), and failure counts (including 429s from rate limiting).

## Files

| File | Purpose |
|------|---------|
| `loadtesting/dummy_backend.py` | Fake OpenAI-compatible LLM server on port 9999 (`uv run dummy`) |
| `loadtesting/locustfile.py` | Locust user behavior (`uv run locust`) |
| `loadtesting/runner.py` | Wrapper that injects the default locustfile path |
| `loadtesting/setup_users.py` | Script to create test entities and API keys in Lumen |
| `loadtesting/config.yaml` | Test configuration (endpoint, model, prompt, keys) |
