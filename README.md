# Lumen

Lumen is a self-hosted AI gateway. It provides a web chat interface and an OpenAI-compatible API proxy, while giving administrators control over who can access which models and how many tokens each user or group can spend. Administrators can configure the system to proxy different models, and for each model it can point to one or more endpoints that host the model.

**Key features:**
- Web chat interface for AI models (OpenAI-compatible endpoints, Ollama, vLLM, etc.)
- OpenAI-compatible API proxy — use Lumen as a drop-in endpoint from any tool or script
- Projects (machine-to-machine accounts) with their own coin pools
- File and document uploads in chat (text, PDF, images — configurable per deployment)
- Login via your institution's identity provider through CILogon
- Token budgets per user and group — with optional auto-refresh
- Ownership-based model access: models are available to everyone unless an owner is assigned, then only the owner can use them; models can additionally require a one-time user acknowledgment
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
version: 3

app:
  secret_key: "replace-with-a-random-value-at-least-32-characters"
  encryption_key: "use-a-different-random-value-at-least-32-characters"
  database:
    url: sqlite:///lumen_dev.db
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

> **Do not run `uv run flask db upgrade` with SQLite.** The migrations are PostgreSQL-only (several use `ALTER TABLE … ADD/DROP CONSTRAINT`, which SQLite does not support). Running `flask db upgrade` against SQLite will fail partway through, leaving the database in a partially-migrated state. The command is gated and will exit with an error if attempted.

For local SQLite development, create the schema directly from the ORM models and stamp the migration head:

```bash
BACKGROUND_WORKER=false uv run python -c \
  "from lumen import create_app; from lumen.extensions import db; \
   app=create_app(); app.app_context().push(); db.create_all()"
uv run flask --app 'lumen:create_app' db stamp head
uv run lumen
```

Visit `http://localhost:5001`, click **Login**, and you'll be auto-logged in as `dev@example.com`.

> **Note:** The `dev_user` option skips OAuth entirely and only works when `app.debug` is `true` (it returns 404 otherwise), so it is safe to leave configured but inert in non-debug deployments. Remove it (or leave it empty) to use normal CILogon authentication.

---

## Operator Commands

Run with `uv run flask --app run <command>` locally, or `flask <command>` inside the container.
Both commands below need PostgreSQL/TimescaleDB; on SQLite they print a note and exit 0.

**`request_logs` is never pruned.** Lumen ships no retention policy and no command that deletes from it.

### `backfill-aggregate`

Materialises a continuous aggregate's full history, month by month, oldest first.

```bash
flask backfill-aggregate [--name NAME] [--from YYYY-MM] [--force]
```

| Flag | Meaning |
|---|---|
| `--name` | Aggregate to refresh. Default `request_counts_hourly_by_entity`. |
| `--from` | First month to refresh. Default: the month of the oldest `request_logs` row. |
| `--force` | Refresh months before a retention boundary anyway, erasing their *aggregate* rows. Raw rows are never touched. |

Migrations create their aggregates `WITH NO DATA`, and the refresh policy only ever materialises its
own `start_offset` window — so per-user "All Time" charts stay near-empty until this is run: they read
only `request_counts_hourly_by_entity` and no longer fall back to raw `request_logs`. **Run it
right after `flask db upgrade` creates a new aggregate, and check the printed row counts.** It walks
month by month because one `CALL refresh_continuous_aggregate(NULL, NULL)` over a production
hypertable is a single long transaction with unbounded memory, and it never refreshes past `now()`,
which would materialise the current bucket and hide every request logged after the backfill.

The retention refusal only fires if someone added a retention policy to `request_logs` by hand:
refreshing a window whose raw chunks were dropped recomputes it as **empty and deletes the
materialised rows, with no error**. The command refuses such a window and prints how many aggregate
rows it would have erased; `--force` overrides the refusal and erases them.

TimescaleDB refuses an *overlapping* refresh outright rather than queueing behind it, so a month
landing while a scheduled policy job is mid-refresh fails immediately with `SQLSTATE 55P03`
(`LockNotAvailable`). `request_metrics_1m`'s policy fires every minute, so a long backfill will meet
one; each month is retried for up to 60 seconds and the line reports `(after N lock retries)`. Every
month that succeeds is printed with `OK`, and a month that fails outright prints the exact
`--from` to resume with. Re-running an already-refreshed month is safe: it recomputes that window
from the raw rows and overwrites it, so resuming never double-counts.

### `check-aggregate`

Read-only: reports whether `request_counts_hourly_by_entity` covers all of `request_logs`.

```bash
flask check-aggregate
```

It prints the aggregate's earliest *materialised* bucket (read from its materialisation hypertable,
not the view, which answers from raw for anything never refreshed) and the oldest `request_logs`
row. It exits 1 when the aggregate is empty or starts after the oldest raw row, printing the
`flask backfill-aggregate --from YYYY-MM` that fixes it; until then the per-user charts are missing
that history. It also exits 1 if a retention policy exists on `request_logs`, and
prints the `SELECT remove_retention_policy('request_logs');` that removes it. Run it before
upgrading.

### Upgrading TimescaleDB

TimescaleDB is required: Lumen does not run on a plain `postgres` image. Pin the image tag
(`timescale/timescaledb:2.27.2-pg17` is the tested version) and upgrade on purpose. A new image
only installs the new extension files; the database keeps running the old version until it is
updated, and nothing in Lumen updates it for you.

1. Bump the image tag (`postgresql.image` in the Helm chart, `image:` in `docker-compose.yml`) and
   restart the database.
2. Update the extension. This must be the first command in a fresh session (`-X` skips `.psqlrc`),
   run as the owner of the extension (normally the user that ran `flask db upgrade`). `lumen` is
   the example database name; use the one in your database URL:

   ```bash
   psql -X -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'
   ```

3. Restart Lumen.
4. Confirm the startup log no longer shows `TimescaleDB extension is <old> but the server has <new>`,
   and that `lumen_timescaledb_extension_update_available` on `/metrics` is `0`
   (`SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';` shows the new version).
5. Run `flask check-aggregate`; it must exit 0.

At startup, and then periodically in every worker process (see "Prometheus metrics" for the
interval), Lumen checks the extension version, the `request_logs` hypertable, the continuous
aggregates and their background jobs. It logs a WARNING per problem (ERROR when the extension or
the hypertable is missing, or when `request_logs` has a retention policy) and never blocks startup.

---

## Configuration Reference (`config.yaml`)

The config file must declare `version: 3` at the top level — the app refuses to start on older config versions. Version 3 removed the `users:`, `projects:`, `clients:`, and `groups:` sections: groups, memberships, and coin pools are managed in the database, model access is ownership-based (managed on each model's detail page), and OAuth auto-assignment rules are managed in the database on each group's Rules tab (there is no `group_rules:` config section).

### App settings

```yaml
app:
  name: Lumen
  tagline: Illuminating AI access
  secret_key: replace-with-a-random-value-at-least-32-characters  # session signing; `openssl rand -hex 32`
  encryption_key: use-a-different-random-value-at-least-32-characters  # API-key hashing and credential encryption
  database:
    url: sqlite:///lumen.db                   # or a postgres:// URL
  debug: false
  theme: illinois   # built-in themes: default, illinois, uic, uis
```

The `theme` key selects the institutional look and feel. Themes live in `themes/<name>/` and can override templates, static assets, and navigation. If the named theme is not found, Lumen falls back to `default`.

The following secrets can be supplied via environment variables, which take precedence over values in `config.yaml`. This is useful for injecting secrets at deploy time (e.g. via Docker secrets or a Kubernetes secret) without writing them into the config file.

| Environment variable | Overrides config key |
|---|---|
| `LUMEN_SECRET_KEY` | `app.secret_key` |
| `LUMEN_ENCRYPTION_KEY` | `app.encryption_key` |
| `OAUTH2_CLIENT_ID` | `oauth2.client_id` |
| `OAUTH2_CLIENT_SECRET` | `oauth2.client_secret` |
| `OAUTH2_SERVER_METADATA_URL` | `oauth2.server_metadata_url` |
| `OAUTH2_SCOPES` | `oauth2.scopes` |

> **Warning:** Rotating `encryption_key` (or `LUMEN_ENCRYPTION_KEY`) invalidates all existing user API keys — users will need to generate new ones. It also makes the endpoint API keys encrypted in the database unreadable until the next startup, when they are re-synced (re-encrypted) from `config.yaml`.

### Authentication

```yaml
oauth2:
  client_id: cilogon:/client_id/...
  client_secret: ...
  server_metadata_url: https://cilogon.org/.well-known/openid-configuration
  scopes: openid email profile org.cilogon.userinfo
  # Optional: restrict login to one institution
  # params:
  #   idphint: urn:mace:incommon:uiuc.edu
```

`oauth2.client_id` and `oauth2.client_secret` can also be supplied via `OAUTH2_CLIENT_ID` and `OAUTH2_CLIENT_SECRET` environment variables (see table above).

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
    input_cost_per_million: 5.0    # for usage tracking only
    output_cost_per_million: 15.0
    description: "OpenAI GPT-4o"   # optional short description shown in the UI
    url: https://huggingface.co/... # optional link shown in model details; HuggingFace URLs also load the model README; a bare repo id (org/name) expands to huggingface.co
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
    input_cost_per_million: 0.0
    output_cost_per_million: 0.0
    endpoints:
      - url: http://localhost:11434/v1
        api_key: ollama
        model: llama3.2
```

Set `disabled: true` to hide a model without removing it. A model can also require a one-time user acknowledgment before use (`needs_ack: true`, with an optional per-model `ack_message`), be flagged `early_access: true`, or carry an `end_date` after which it is hidden and rejected.

### Model access control

Model access is **ownership-based** and managed in the web UI, not in `config.yaml`. For each user or project and each model:

1. A `disabled` model or one past its `end_date` is blocked for everyone.
2. A model **without an owner** is available to everyone (users and projects).
3. An **owned** model is available only to its owner.
4. A visible model with `needs_ack` or `early_access` requires a one-time acknowledgment before use.

Admins assign the owner from the **Access** card on the model's detail page (`/models/<name>`): pick an owner via the user typeahead, or click **Make public** to clear it.

### Groups and coin budgets

Groups control how many coins users can spend. Coins map to cost in USD (e.g. 1 coin ≈ $1 of model usage at your configured rates). Groups, their memberships, and their coin pools live in the database. Per-user pools are set from the Edit dialog on a user's profile (admin only); when a user has no pool of their own, the most generous group pool applies (`-2` unlimited beats any positive value), falling back to the global `defaults.tokens` pool.

#### Auto-assignment rules

Auto-join rules add users to a group at login based on their CILogon attributes (requires the `org.cilogon.userinfo` scope). Rules live in the database and are edited on each group's admin-only **Rules** tab — field, contains/equals matcher, and value. A user must match **all** rules of a group to be added, and is removed again when they stop matching on a later login. There is no `group_rules:` config section.

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

All endpoints are rate-limited per authenticated user (API key ID for `/v1/*` routes, session user ID for `/chat/*` routes), except the read-only `GET /v1/models` and `GET /v1/models/{id}` lookups, which are not counted. The limit is a single string in flask-limiter notation (`N per second/minute/hour`):

```yaml
rate_limiting:
  limit: "30 per minute"
  # storage_url: redis://localhost:6379/0  # optional; use Redis in multi-worker deployments
```

By default, limits are tracked in-memory (per-process). For multi-worker deployments (e.g. uvicorn with `--workers` > 1), set `storage_url` to a shared Redis instance so limits are enforced across all workers. Changing `storage_url` requires a restart; changing `limit` takes effect within ~5 seconds (hot-reloaded).

### Request concurrency

Lumen is a WSGI app served through `a2wsgi` under uvicorn, so each worker process serves requests from a thread pool. Requests beyond the pool size wait until a thread frees up. `LUMEN_WSGI_WORKERS` sets the pool size (Helm: `wsgiWorkers`) and defaults to `auto`, which derives it from this process's connection pool; a positive integer sets it directly:

```bash
LUMEN_WSGI_WORKERS=32 uvicorn asgi:app --host 0.0.0.0 --port 5001
```

`auto` sets the thread count to 2 × (`pool_size + max_overflow`) (see below), clamped to 10–256. Streaming requests release their database connection before the upstream LLM call, so most busy threads hold no connection and the pool can serve twice as many threads as it has connections. If non-streaming database work ever saturates the pool, the extra requests wait up to `pool_timeout` for a connection; set a number to size it yourself. Because the pool is itself divided across workers × replicas, `auto` scales the thread count down as you add processes or pods.

`auto` falls back to 10 only when the pool is unsized: on SQLite, or on PostgreSQL when `max_connections` can't be queried at startup and no `pool_size` is configured. An explicit `pool_size` (and `max_overflow`) is still used in that case. Any value other than a positive integer or `auto` (for example `0` or `many`) logs a warning once and is treated as `auto`. The resolved count is logged at startup.

Raising threads is usually cheaper than adding worker processes (`--workers` / `WEB_CONCURRENCY`), which divide the same connection budget further and duplicate every per-process cache.

A thread handing a response chunk to the server will wait at most `LUMEN_WSGI_SEND_TIMEOUT` seconds (default 300; Helm: `wsgiSendTimeout`) before declaring the client gone, logging a warning, and releasing itself:

```bash
LUMEN_WSGI_SEND_TIMEOUT=120 uvicorn asgi:app --host 0.0.0.0 --port 5001
```

This exists for clients that stop reading *without* closing the connection — a laptop that sleeps, a dropped NAT entry, an app that opens a stream and abandons it. No disconnect is ever reported for those, and the kernel never abandons a peer that is alive but advertising a zero window, so without a bound the thread is pinned forever. It is a stall detector, not a rate limit: a thread only starts waiting once the socket buffer, the server's write buffer, and the whole send queue are already full, which even a very poor mobile link clears in well under a minute. Keep it below the gateway's request timeout so a stalled thread is reclaimed before the request would have been cut off anyway. Non-numeric or non-positive values warn and fall back to the default — the wait cannot be disabled, because unbounded is the failure this prevents.

### Database connection pool

On PostgreSQL the pool is **auto-sized** from the server's `max_connections`, divided across all worker processes and Kubernetes replicas so combined usage cannot exhaust the server: 60% to `pool_size`, 20% to `max_overflow`, and 20% reserved for psql/migrations/monitoring. Worker count is detected from `WEB_CONCURRENCY` or the uvicorn `--workers` flag; replica count comes from the `LUMEN_REPLICAS` env var (set by the Helm chart from `replicaCount`). Pre-ping is always enabled. SQLite has no connection limit, so sizing is skipped. Changes require a restart.

You can override the auto-sizing under `app.database`:

```yaml
app:
  database:
    url: postgresql://lumen:lumen@localhost:5432/lumen
    # pool_size / max_overflow omitted → auto-sized
    pool_size: 20        # override persistent connections kept open
    max_overflow: 30     # override burst connections allowed above pool_size
    max_connections: 200 # override the detected Postgres max_connections
    pool_timeout: 10     # seconds to wait for a free connection before returning an error
    pool_recycle: 1800   # recycle connections after 30 min to avoid stale-connection errors
```

Explicit `pool_size` / `max_overflow` are honored only if they fit within 80% of `max_connections` across all workers × replicas; otherwise the auto-sized values are used and a warning is logged.

### Projects

Projects are machine-to-machine accounts — scripts, applications, or automated pipelines — that talk to Lumen's OpenAI-compatible API using an API key instead of logging in via OAuth. They are distinct from human users: they have no email address, no web chat access, and no per-user coin budget. Instead, each project has its own coin pool.

**Creating and managing projects**

Admins create projects via the **Projects** page in the web UI (or via the API). Each project has:
- One or more named API keys (generated in the UI, shown once, then hashed)
- A coin pool (balance, cap, and optional hourly refill), set by admins from the Edit dialog on the project detail page
- One or more **managers** — regular users who can view and rotate that project's keys

Managers can see the project's detail page and issue new keys but cannot change budgets. Only admins can create projects, adjust budgets, or assign managers.

**Using a project API key**

Point any OpenAI-compatible tool at Lumen and use the project's API key as the `Authorization: Bearer` token:

```
base_url: https://your-lumen-domain/v1
api_key:  sk_...
```

**Coin pools**

Project coin pools work the same as user coin pools — each request deducts coins based on tokens used at the model's configured rate. The pool recharges at `refresh` coins per hour up to the `max` cap. A project without its own pool falls back to the global `defaults.tokens` pool.

**Model access for projects**

Projects follow the same ownership rules as users: every public (unowned) model is available. For models that require acknowledgment (`needs_ack`), a manager must visit the project's detail page and click **Accept** before the project can use them.

### Monitoring

A read-only token for `GET /v1/models` — useful for uptime checkers that don't have a user account. It lives under the `api:` section:

```yaml
api:
  monitoring:
    token: "a-long-random-string"   # leave empty to disable
```

### Prometheus metrics

Configured under the `api:` section:

```yaml
api:
  prometheus:
    enabled: true
    token: "a-long-random-string"   # optional; Bearer token auth for /metrics
    multiproc_dir: "/tmp/prom"      # required for multi-worker aggregation (mount as shared volume)
```

On PostgreSQL, `/metrics` also reports TimescaleDB health. Each worker process caches its own
check and refreshes it on the metrics snapshot thread every `ceil(300 / I) × I` seconds, where
`I = max(60, 30 × P)` is the snapshot interval and `P` is the number of worker processes per pod
(`WEB_CONCURRENCY`, or `wsgiProcesses` in the chart; `wsgiWorkers` sets threads and does not count).
Each sleep is jittered by ±10%, so the gap between checks can reach 1.1 × that interval plus the
query time:

| Worker processes `P` | Check interval | Longest gap (with jitter) |
|---|---|---|
| 1–2 | 300 s | 330 s |
| 4 | 360 s | 396 s |
| 8 | 480 s | 528 s |
| 9 | 540 s | 594 s |
| 10–20 | `30 × P` (300–600 s) | 660 s at 20 |
| 64 | 1920 s | 2112 s |
| 256 | 7680 s | 8448 s |


| Metric | Labels | Meaning |
|---|---|---|
| `lumen_timescaledb_extension_update_available` | `installed`, `available` | 1 when the database runs an older extension than the image ships (see "Upgrading TimescaleDB") |
| `lumen_timescaledb_hypertable_present` | `table` | 1 when `request_logs` is a hypertable |
| `lumen_timescaledb_aggregate_present` | `view` | 1 when the continuous aggregate exists |
| `lumen_timescaledb_aggregate_materialized_only` | `view` | 1 when the aggregate has `materialized_only = true` (should be 0) |
| `lumen_timescaledb_job_present` | `job` | 1 when the expected background job exists |
| `lumen_timescaledb_job_last_run_failed` | `job` | 1 when the job's last run failed |
| `lumen_timescaledb_job_failures_total` | `job` | Failed runs of the job (counter) |
| `lumen_timescaledb_job_last_success_age_seconds` | `job` | Seconds since the job last finished successfully |
| `lumen_timescaledb_request_logs_retention_policy` | | 1 when `request_logs` has a retention policy (it must never be pruned) |
| `lumen_timescaledb_health_age_seconds` | | Age of the check these gauges come from |

`job` is one of `refresh:request_counts_hourly`, `refresh:request_counts_hourly_by_entity`,
`refresh:request_metrics_1m`, `compression:request_logs` and `retention:request_metrics_1m`.

Suggested alert expressions (the chart ships no alert rules). The two age thresholds below are
sized for **up to 20 worker processes** per pod (longest check gap 660 s), with a margin of
about 1.8×. For more processes, set them from `T = 2 × max(600, 30 × P)` seconds:
`health_age_seconds > T` and `job_last_success_age_seconds{job=~"refresh:.*"} > 7200 + T`.
The job age is computed at scrape time from the cached last success. A stale check can therefore
make it read too high, by up to one check gap, but never too low. The `+ T` keeps a healthy hourly
refresh from firing.

```promql
# Refresh or compression jobs failing. A refresh outage longer than 3 hours leaves a permanent gap in the hourly charts.
increase(lumen_timescaledb_job_failures_total[1h]) > 0
lumen_timescaledb_job_last_run_failed == 1
# Two missed hourly refreshes plus T = 1200 s (up to 20 processes): 7200 + 1200
lumen_timescaledb_job_last_success_age_seconds{job=~"refresh:.*"} > 8400

# Missing or misconfigured objects
lumen_timescaledb_job_present == 0
lumen_timescaledb_hypertable_present == 0
lumen_timescaledb_aggregate_present == 0
lumen_timescaledb_aggregate_materialized_only == 1
lumen_timescaledb_request_logs_retention_policy == 1

# Extension older than the image (warning, not paging)
lumen_timescaledb_extension_update_available == 1

# The health check itself stopped refreshing. T = 1200 s (up to 20 processes)
lumen_timescaledb_health_age_seconds > 1200
```

The failure alerts show a failing job within one check gap. A job that stops running without
failing shows up only through the age alert. Above 60 worker processes, `7200 + T` passes
the 3-hour window in which a refresh outage can still be recovered.

## License

Lumen is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
