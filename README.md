# Lumen

Lumen is a self-hosted AI gateway for research institutions. It provides a web chat interface and an OpenAI-compatible API proxy, while giving administrators control over who can access which models and how many tokens each user or group can spend.

**Key features:**
- Web chat interface for AI models (OpenAI-compatible endpoints, Ollama, vLLM, etc.)
- OpenAI-compatible API proxy — use Lumen as a drop-in endpoint from any tool or script
- Clients (machine-to-machine accounts) with their own coin pools and model access rules
- File and document uploads in chat (text, PDF, images — configurable per deployment)
- Login via your institution's identity provider through CILogon
- Token budgets per user and group — with optional auto-refresh
- Per-model access control: whitelist, blacklist, and graylist (requires user acknowledgment)
- Admin panel to manage users, groups, usage, and analytics charts
- Institutional theming (built-in: `default`, `illinois`, `uic`, `uis`)
- Round-robin load balancing across multiple model backends
- Prometheus metrics endpoint

---

## Getting Started

### 1. Requirements

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A public domain name (required for CILogon OAuth)

### 2. Get CILogon credentials

CILogon provides federated login for research institutions (universities, national labs, etc.).

1. Register your application at https://cilogon.org/oauth2/register
2. Set the callback URL to `https://your-domain/callback`
3. Request these scopes: `openid email profile org.cilogon.userinfo`
4. Note your `client_id` and `client_secret`

### 3. Configure

Copy the example config and edit it:

```bash
cp config.yaml.example lumen/config.yaml
```

At minimum, set:
- `app.secret_key` — a long random string
- `oauth2.client_id` and `oauth2.client_secret` — from CILogon
- `oauth2.redirect_uri` — `https://your-domain/callback`
- `admins` — your email address
- `models` — at least one model endpoint (see below)

### 4. Start the stack

```bash
docker compose up -d
```

Lumen will be available at `https://your-domain`.

---

## Local Development

If you want to run Lumen locally without Docker or CILogon credentials:

### 1. Install dependencies

```bash
uv sync
```

### 2. Create a local config

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with at minimum:

```yaml
app:
  secret_key: "any-random-string"
  encryption_key: "another-random-string"
  database_url: sqlite:///lumen_dev.db
  debug: true
  dev_user:                    # bypasses OAuth — logs in as this email automatically
    email: dev@example.com
    groups:                    # optional: assign groups on every dev login
      - staff
```

And at least one model under `models:`. Two options:

**Option A: Built-in echo server** (no external dependencies)

The repo includes a lightweight echo server that mirrors your message back with sample math. Add this to your `config.yaml`:

```yaml
models:
  - name: dummy
    active: true
    input_cost_per_million: 0.0
    output_cost_per_million: 0.0
    endpoints:
      - url: http://localhost:9999/v1
        api_key: dummy
```

Start it in a separate terminal before running Lumen:

```bash
uv run dummy
```

**Option B: Ollama** (real local models)

Install [Ollama](https://ollama.ai), pull a model, and keep the `llama3` entry in `config.yaml` pointing at `http://localhost:11434/v1`:

```bash
ollama pull llama3.2
```

### 3. Initialize the database and start

```bash
uv run flask db upgrade
uv run lumen
```

Visit `http://localhost:5000`, click **Login**, and you'll be auto-logged in as `dev@example.com`.

> **Note:** The `dev_user` option skips OAuth entirely. Remove it (or leave it empty) to use normal CILogon authentication.

---

## Configuration Reference (`config.yaml`)

### App settings

```yaml
app:
  name: Lumen
  tagline: Illuminating AI access
  secret_key: change-me-to-something-random   # any long random string; used for session cookies
  encryption_key: change-me-to-something-different  # separate secret used to hash user API keys
  database_url: sqlite:///lumen.db            # or a postgres:// URL
  debug: false
  theme: illinois   # built-in themes: default, illinois, uic, uis
```

The `theme` key selects the institutional look and feel. Themes live in `themes/<name>/` and can override templates, static assets, and navigation. If the named theme is not found, Lumen falls back to `default`.

`encryption_key` can also be supplied via the `LUMEN_ENCRYPTION_KEY` environment variable, which takes precedence over the value in `config.yaml`. This is useful for injecting secrets at deploy time (e.g. via Docker secrets or a Kubernetes secret) without writing them into the config file.

> **Warning:** Rotating `encryption_key` (or `LUMEN_ENCRYPTION_KEY`) invalidates all existing user API keys — users will need to generate new ones.

### Authentication

```yaml
oauth2:
  client_id: cilogon:/client_id/...
  client_secret: ...
  server_metadata_url: https://cilogon.org/.well-known/openid-configuration
  redirect_uri: https://your-domain/callback
  scopes: openid email profile org.cilogon.userinfo
  # Optional: restrict login to one institution
  # params:
  #   idphint: urn:mace:incommon:uiuc.edu
```

### Admins

```yaml
admins:
  - you@example.edu
```

Admins have full access to the admin panel (users, groups, usage stats).

### Models

Each model entry defines a name users will see and one or more backend endpoints. Lumen round-robins across endpoints and skips unhealthy ones.

```yaml
models:
  - name: gpt-4o
    active: true
    input_cost_per_million: 5.0    # for usage tracking only
    output_cost_per_million: 15.0
    description: "OpenAI GPT-4o"   # optional short description shown in the UI
    url: https://huggingface.co/... # optional link shown in model details; HuggingFace URLs also load the model README
    knowledge_cutoff: "2024-04"    # optional, shown in model details
    supports_reasoning: false      # set true to stream chain-of-thought tokens
    supports_function_calling: true # optional, shown in model details
    input_modalities: ["text", "image"]   # optional, shown in model details
    output_modalities: ["text"]
    context_window: 128000         # optional token limit shown in model details
    max_output_tokens: 4096        # optional
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-...
        # model: gpt-4o            # optional — overrides the name sent to this endpoint

  - name: llama3
    active: true
    input_cost_per_million: 0.0
    output_cost_per_million: 0.0
    endpoints:
      - url: http://localhost:11434/v1
        api_key: ollama
        model: llama3.2
```

Set `active: false` to hide a model without removing it.

### Model access control

Lumen supports three access levels for each model:

| Level | Meaning |
|-------|---------|
| **whitelist** | Explicitly allowed — no acknowledgment required |
| **graylist** | Visible to users, but requires a one-time acknowledgment before use |
| **blacklist** | Blocked — model is hidden from users |

Access is resolved in this order for each user + model combination:

1. **User override** (admin-set per-user rule) — wins over everything else
2. **Group per-model rules** — blacklist beats whitelist beats graylist
3. **Effective default** — most permissive group `model_access.default` wins; falls back to `allowed`

#### Per-group model access

Each group can define its own `model_access:` section:

```yaml
groups:
  restricted:
    model_access:
      default: blacklist        # deny all models for this group
      whitelist: [safe-a, safe-b]

  vip:
    model_access:
      whitelist: [experimental] # VIP users skip graylist acknowledgment

  all-allowed:
    model_access:
      default: whitelist        # allow everything for this group
```

When a user belongs to multiple groups, the **most permissive default wins** (e.g. if one group has `default: whitelist` and another has `default: blacklist`, the user gets whitelist). For per-model rules, blacklist always beats whitelist/graylist.

### Groups and coin budgets

Groups control how many coins users can spend. Coins map to cost in USD (e.g. 1 coin ≈ $1 of model usage at your configured rates). Every user gets the `default` group automatically. You can create additional groups and assign users manually via the admin panel, or auto-assign them based on CILogon attributes.

```yaml
groups:
  default:
    max: 0          # coin budget (0 = blocked, -2 = unlimited)
    refresh: 0      # coins added per hour (0 = no auto-refresh)
    starting: 0     # coins granted on first login

  faculty:
    max: 50         # $50 total budget
    refresh: 0.5    # $0.50/hr auto-refresh
    starting: 10    # $10 on first login
```

#### Auto-assignment rules

Automatically add users to a group at login based on their CILogon attributes (requires the `org.cilogon.userinfo` scope):

```yaml
groups:
  staff:
    rules:
      - field: affiliation
        contains: staff@illinois.edu   # substring match
      - field: idp
        equals: urn:mace:incommon:uiuc.edu   # exact match
    max: 20
    refresh: 0.05
    starting: 20
```

Supported fields: `affiliation`, `member_of`, `idp`, `ou`. Groups assigned by rules are automatically removed if the rule no longer matches on next login.

### Chat settings

```yaml
chat:
  remove: hide   # "hide" = soft-delete (recoverable) | "delete" = permanent
  upload:
    max_size_mb: 10           # maximum file upload size
    max_text_chars: 100000    # maximum extracted text before truncation
    allowed_extensions:       # accepted file types (backend uses magic-byte detection)
      - txt
      - md
      - csv
      - json
      - py
      - pdf
      - png
      - jpg
      - jpeg
```

### Rate limiting

All endpoints are rate-limited per authenticated user (API key ID for `/v1/*` routes, session user ID for `/chat/*` routes). The limit is a single string in flask-limiter notation (`N per second/minute/hour`):

```yaml
rate_limiting:
  limit: "30 per minute"
  # storage_url: redis://localhost:6379/0  # optional; use Redis in multi-worker deployments
```

By default, limits are tracked in-memory (per-process). For multi-worker deployments (e.g. gunicorn with multiple workers), set `storage_url` to a shared Redis instance so limits are enforced across all workers. Changing `storage_url` requires a restart; changing `limit` takes effect within ~5 seconds (hot-reloaded).

### Database connection pool

Controls how SQLAlchemy manages database connections. The defaults (pool size 5, overflow 10) are fine for light use; increase them for production or high-concurrency workloads. Changes require a restart.

```yaml
app:
  db_pool:
    pool_size: 20        # persistent connections kept open
    max_overflow: 30     # burst connections allowed above pool_size
    pool_timeout: 10     # seconds to wait for a free connection before returning an error
    pool_recycle: 1800   # recycle connections after 30 min to avoid stale-connection errors
    pool_pre_ping: true  # test each connection before use; silently replaces stale ones
```

`pool_size + max_overflow` is the maximum number of simultaneous DB connections. For 50 concurrent requests, set these to at least 50 combined.

### Clients

Clients are machine-to-machine accounts — scripts, applications, or automated pipelines — that talk to Lumen's OpenAI-compatible API using an API key instead of logging in via OAuth. They are distinct from human users: they have no email address, no web chat access, and no per-user coin budget. Instead, each client has its own coin pool and model access rules.

**Creating and managing clients**

Admins create clients via the **Clients** page in the web UI (or via the API). Each client has:
- One or more named API keys (generated in the UI, shown once, then hashed)
- A coin pool (balance, cap, and optional hourly refill)
- A model access policy (whitelist / blacklist / graylist)
- One or more **managers** — regular users who can view and rotate that client's keys

Managers can see the client's detail page and issue new keys but cannot change budgets or model access. Only admins can create clients, adjust budgets, or assign managers.

**Using a client API key**

Point any OpenAI-compatible tool at Lumen and use the client's API key as the `Authorization: Bearer` token:

```
base_url: https://your-lumen-domain/v1
api_key:  lmk-...
```

**Coin pools**

Client coin pools work the same as user coin pools — each request deducts coins based on tokens used at the model's configured rate. The pool recharges at `refresh` coins per hour up to the `max` cap.

**Default coin pool from config**

The `clients:` block in `config.yaml` sets the default pool parameters for all clients and optional named overrides:

```yaml
clients:
  default:
    max: 100.0        # coin budget (-2 = unlimited, 0 = blocked)
    refresh: 0.0      # coins added per hour
    starting: 100.0   # coins when the pool is first created
    model_access:
      default: whitelist   # allow all models unless explicitly listed

  research-bot:            # named override for this specific client
    max: 500.0
    refresh: 1.0
    starting: 500.0
    model_access:
      default: blacklist   # deny all models not in whitelist
      whitelist: [gpt-4o, llama3]
```

Named entries match on the client's name as set in the UI. If a client has no named entry, `default` applies. Changes to `config.yaml` do **not** retroactively update existing coin pools — pool parameters are written to the database when the pool is first created.

**Model access for clients**

Clients follow the same whitelist / blacklist / graylist rules as users. Clients cannot be assigned graylist directly; a manager must visit the client's detail page and click **Accept** on any graylisted model before the client can use it.

### Monitoring

A read-only token for `GET /v1/models` — useful for uptime checkers that don't have a user account:

```yaml
monitoring:
  token: "a-long-random-string"   # leave empty to disable
```

### Prometheus metrics

```yaml
prometheus:
  enabled: true
  token: "a-long-random-string"   # optional; Bearer token auth for /metrics
  multiproc_dir: "/tmp/prom"      # required for multi-worker aggregation (mount as shared volume)
```

