# Load Testing

> **Developer/operator note:** This guide is for testing Lumen under load. It requires access to the database and a running Lumen instance.

The `loadtesting/` directory contains a [Locust](https://locust.io)-based load test and a helper script to provision test client accounts.

## Prerequisites

- Lumen installed and its database initialized (run `uv run flask db upgrade` at least once)
- `uv` available in your PATH
- A `loadtesting/config.yaml` file (see below)

---

## Quick Start

### 1. Start the dummy backend (optional, for local testing)

The dummy backend simulates an OpenAI-compatible model endpoint without hitting a real LLM:

```bash
uv run dummy
```

This starts a mock server on `http://localhost:9999`. Configure a model in `config.yaml` that points to it.

### 2. Create load-test client accounts

```bash
# Create 10 clients with 20 coins each for the "dummy" model
uv run python loadtesting/setup_users.py 10

# Custom model or coin budget
uv run python loadtesting/setup_users.py 10 --model my-model --coins 100

# Write the generated API keys directly into loadtesting/config.yaml
uv run python loadtesting/setup_users.py 10 --write-config
```

`setup_users.py` options:

| Flag | Default | Description |
|------|---------|-------------|
| `count` | *(required)* | Number of client accounts to create |
| `--model` | `dummy` | Model name to grant access to |
| `--coins` | `20` | Coins granted per client |
| `--prefix` | `loadtest` | Name prefix for created entities (e.g. `loadtest-1`) |
| `--write-config` | off | Update `loadtesting/config.yaml` with the generated keys |

### 3. Configure `loadtesting/config.yaml`

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
| `base_url` | URL of the Lumen instance to test. On macOS use `http://127.0.0.1:5001` — AirPlay Receiver occupies `localhost:5000` on Monterey and later and returns a bare 403. |
| `model` | Model name sent in each request |
| `api_keys` | List of API keys; users are assigned round-robin |
| `questions` | Mix of question types: `static` (from `prompts`) and/or `math` (randomly generated) |
| `prompts` | Static prompt strings used when `questions` includes `static` |

### 4. Run the load test

```bash
# Interactive web UI at http://localhost:8089
uv run locust

# Headless: 10 users, ramp 2/s, run for 60 seconds
uv run locust --headless -u 10 -r 2 --run-time 60s
```

---

## How It Works

Each simulated Locust user is assigned one API key (round-robin). On each task it sends a `POST /v1/chat/completions` request with a randomly selected prompt. Responses are checked for HTTP 200; rate-limit (429) responses are reported as failures.

Having N API keys gives you up to N × (per-key rate limit) effective throughput before rate limiting kicks in.

---

## Cleanup

Load-test accounts are named `loadtest-1`, `loadtest-2`, etc. by default. Remove them via the Lumen admin UI or directly in the database.
