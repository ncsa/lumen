# Lumen

Lumen is a self-hosted AI chat portal for research institutions. It lets your users chat with AI models through a web browser, while giving administrators control over who can access which models and how many tokens each user or group can spend.

**Key features:**
- Chat interface for AI models (OpenAI-compatible endpoints, Ollama, vLLM, etc.)
- Login via your institution's identity provider through CILogon
- Token budgets per user and group — with optional auto-refresh
- Per-model access control: whitelist, blacklist, and graylist (requires user acknowledgment)
- Admin panel to manage users, groups, and usage
- Round-robin load balancing across multiple model backends

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
```

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
    url: https://huggingface.co/... # optional HuggingFace URL — enables README tab on model page
    knowledge_cutoff: "2024-04"    # optional, shown in model details
    supports_reasoning: false      # set true to stream chain-of-thought tokens
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
2. **Global blacklist** — absolute; no group can override it
3. **Group per-model rules** — blacklist beats whitelist beats graylist
4. **Global per-model rules** (graylist / whitelist)
5. **Effective default** — most permissive group `model_access.default` wins; falls back to global `model_access.default`

#### Global model access

```yaml
model_access:
  default: whitelist   # default for models not listed: whitelist|blacklist|graylist (default: whitelist)
  blacklist:
    - old-model        # always blocked for everyone
  graylist:
    - experimental     # requires one-time user acknowledgment
  whitelist:
    - safe-model       # always allowed, no acknowledgment ever
```

Use `*` as a shorthand for setting the default:

```yaml
model_access:
  blacklist: ["*"]   # same as: default: blacklist
```

#### Per-group model access

Each group can define its own `model_access:` with the same structure:

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

