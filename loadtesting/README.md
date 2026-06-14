# Lumen Load Testing

A [Locust](https://locust.io)-based load-testing toolkit for the Lumen API. It uses a dummy backend so
you can benchmark the full auth / coin / routing stack without hitting real models.

All commands below are run from the **project root**.

## Prerequisites

- Lumen installed (`uv sync`) and its database initialized (`uv run flask db upgrade` at least once).
- `uv` available on your `PATH`.
- A model in `config.yaml` that points at the dummy backend (see step 2).

## One-command local stack

`run_loadtest.sh` resets the database, starts the dummy backend and Lumen (uvicorn, 4 workers),
provisions load-test accounts, and opens Locust:

```bash
./loadtesting/run_loadtest.sh [USERS] [MODEL] [CONFIG_YAML]
# defaults: 500 users, model "dummy", config ./config.yaml
```

Ctrl-C stops everything cleanly. The rest of this document covers the manual steps that script automates.

## Manual setup

### 1. Add the dummy model to `config.yaml` (one-time)

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

### 2. Start the dummy backend

The dummy backend mocks an OpenAI-compatible endpoint on `http://localhost:9999` and responds instantly
with fixed token counts, so measured latency reflects Lumen overhead only:

```bash
uv run dummy
```

### 3. Create load-test accounts

`setup_users.py` provisions client accounts and API keys directly in the database:

```bash
# Create 10 accounts with 20 coins each for the "dummy" model
uv run python loadtesting/setup_users.py 10

# Custom model and coin budget
uv run python loadtesting/setup_users.py 10 --model my-model --coins 100

# Write the generated API keys straight into loadtesting/config.yaml
uv run python loadtesting/setup_users.py 10 --write-config
```

| Flag | Default | Description |
|------|---------|-------------|
| `count` | *(required)* | Number of accounts to create |
| `--model` | `dummy` | Model name to grant access to |
| `--coins` | `20` | Coins granted per account |
| `--prefix` | `loadtest` | Name prefix for created entities (e.g. `loadtest-1`) |
| `--group` | *(none)* | Add each entity to this group (e.g. `staff`) for model access |
| `--write-config` | off | Update `loadtesting/config.yaml` with the generated keys |

### 4. Configure `loadtesting/config.yaml`

```yaml
base_url: http://127.0.0.1:5001
model: dummy
api_keys:
  - sk_...
  - sk_...
questions:
  - static
  - math
prompts:
  - Say hello in one sentence.
  - What is the capital of France?
```

| Key | Description |
|-----|-------------|
| `base_url` | URL of the Lumen instance under test. On macOS use `http://127.0.0.1:5001` — AirPlay Receiver occupies `localhost:5000` on Monterey and later and returns a bare 403. |
| `model` | Model name sent in each request |
| `api_keys` | List of API keys; simulated users are assigned one round-robin |
| `questions` | Mix of question types: `static` (drawn from `prompts`) and/or `math` (randomly generated arithmetic) |
| `prompts` | Static prompt strings used when `questions` includes `static` |

### 5. Run the load test

```bash
# Interactive web UI at http://localhost:8089
uv run locust

# Headless: 10 users, ramp 2/s, run for 60 seconds
uv run locust --headless -u 10 -r 2 --run-time 60s
```

Key flags:

| Flag | Description |
|------|-------------|
| `-u N` | Number of concurrent users |
| `-r N` | Users spawned per second |
| `--run-time 60s` | Stop after this duration |
| `--headless` | No web UI; print stats to the terminal |
| `--host URL` | Override `base_url` from config |

## How it works

- Each simulated Locust user is assigned one API key (round-robin) and sends `POST /v1/chat/completions`
  with a randomly selected prompt on each task.
- Responses are checked for HTTP 200; rate-limit (429) responses are reported as failures.
- With N keys the effective ceiling is N × the per-key rate limit (Lumen's default is `30 per minute`)
  before rate limiting kicks in.

## Running at high concurrency (macOS)

With 500+ concurrent users, macOS's default open-file limit (256) causes
`OSError: [Errno 24] Too many open files`. Raise it before starting Lumen, and prefer uvicorn over the
Flask dev server:

```bash
ulimit -n 65536
uv run uvicorn run:app --host 127.0.0.1 --port 5001 --workers 4 --interface wsgi
```

## Files

| File | Purpose |
|------|---------|
| `dummy_backend.py` | Fake OpenAI-compatible LLM server on port 9999 (`uv run dummy`) |
| `locustfile.py` | Locust user behavior (`uv run locust`) |
| `runner.py` | Wrapper that injects the default locustfile path |
| `setup_users.py` | Creates test entities and API keys in Lumen |
| `run_loadtest.sh` | One-command local stack (DB reset → backend → Lumen → users → Locust) |
| `config.yaml` | Test configuration (endpoint, model, prompts, keys) |

## Cleanup

Load-test accounts are named `loadtest-1`, `loadtest-2`, … by default. Remove them via the Lumen admin
UI or directly in the database.
