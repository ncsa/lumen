# Lumen

Lumen is a self-hosted AI chat portal for research institutions. It lets your users chat with AI models through a web browser, while giving administrators control over who can access which models and how many tokens each user or group can spend.

**Key features:**
- Chat interface for AI models (OpenAI-compatible endpoints, Ollama, vLLM, etc.)
- Login via your institution's identity provider through CILogon
- Token budgets per user and group — with optional auto-refresh
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

### Groups and token budgets

Groups control how many tokens users can spend. Every user gets the `default` group. You can create additional groups and assign users manually via the admin panel, or auto-assign them based on CILogon attributes.

```yaml
groups:
  default:
    default:          # applies to all models
      max: 0          # token budget (0 = no access)
      refresh: 0      # tokens added per hour (0 = no auto-refresh)
      starting: 0     # tokens granted on first login

  faculty:
    default:
      max: 1000000
      refresh: 50000
      starting: 1000000
```

#### Auto-assignment rules

Automatically add users to a group at login based on their CILogon attributes (requires the `org.cilogon.userinfo` scope):

```yaml
groups:
  uiuc-staff:
    rules:
      - field: affiliation
        contains: staff@illinois.edu   # substring match
      - field: idp
        equals: urn:mace:incommon:uiuc.edu   # exact match
    default:
      max: 500000
      refresh: 10000
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

