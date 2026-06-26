# Application Settings

> 🔒 **Admin only.** This page documents administrator features. Configuration lives in `config.yaml` and the in-app Config editor (`/admin/config`), which are only available to administrators.

These are the core settings that make Lumen work: database, authentication, and general behavior.

## app

| Field | Description |
|-------|-------------|
| `name` | Display name shown in the app header |
| `tagline` | Subtitle shown next to the name |
| `secret_key` | Flask session encryption key (see Security Notes below) |
| `encryption_key` | Used to hash API keys stored in the database. See **Security Notes** |
| `database.url` | SQLAlchemy connection URL — PostgreSQL (`postgresql://...`) in production, or SQLite (`sqlite:///lumen.db`) for local development |
| `debug` | Enable debug mode (set to `false` in production) |
| `theme` | Institutional theme. Themes live in `themes/<name>/`. Built-in: `default`, `illinois`, `uic`, `uis`. Falls back to `default` if not found. |
| `github_url` | Optional — overrides the default GitHub link in the navbar |
| `config_editor` | Optional (default: `true`) — when `false`, the `/admin/config` editor is read-only. Set this for git-managed configs that should only change through version control. The Helm chart sets it to `false`. |

> The global acknowledgement message has moved to `defaults.models.ack_message` (see [Admin Configuration](config.md#global-defaults)). The legacy `app.graylist_default_notice` is still accepted as input and feeds `defaults.models.ack_message`.

### Database Pool

On PostgreSQL the connection pool is **auto-sized** from the server's
`max_connections`, divided across all worker processes and Kubernetes replicas so
the combined usage cannot exhaust the server:

- `pool_size` per process = 60% of `max_connections` ÷ (workers × replicas)
- `max_overflow` per process = 20% of `max_connections` ÷ (workers × replicas)
- the remaining 20% is reserved for psql, migrations, monitoring, etc.

Worker count is detected from `WEB_CONCURRENCY` or the uvicorn `--workers` flag;
replica count comes from the `LUMEN_REPLICAS` env var (set automatically by the Helm
chart from `replicaCount`). Pre-ping is always enabled to silently replace stale
connections. SQLite has no connection limit, so pool sizing is skipped for it.

Optional overrides under `app.database`:

| Field | Description | Default |
|-------|-------------|---------|
| `pool_size` | Override persistent connections per process | auto-sized |
| `max_overflow` | Override burst connections above `pool_size` per process | auto-sized |
| `max_connections` | Override the detected Postgres `max_connections` (skips the `SHOW max_connections` query) | queried at startup |
| `pool_timeout` | Seconds to wait before raising a timeout error | 30 |
| `pool_recycle` | Recycle connections after N seconds to avoid stale-connection errors | N/A (no recycling) |

Explicit `pool_size` / `max_overflow` are honored only if they fit within 80% of
`max_connections` across all workers × replicas; otherwise they are ignored and the
auto-sized values are used (a warning is logged).

```yaml
app:
  database:
    url: postgresql://lumen:lumen@localhost:5432/lumen
    # pool_size / max_overflow omitted → auto-sized
    pool_timeout: 30
    pool_recycle: 1800
```

### Development User

For local development only. When `dev_user` is set:

```yaml
app:
  dev_user:
    email: dev@example.com
    groups:
      - staff
```

The specified email logs in directly without going through the OAuth identity provider. It also assigns the listed groups to that user. Should not be used in production.

### Logging

Optional settings under `app.logs`:

| Field | Description | Default |
|-------|-------------|---------|
| `level` | Log level — `debug`, `info`, `warning`, `error` | `debug` in development, `info` in production |
| `access` | Log HTTP request details | `true` |
| `model` | Log per-endpoint health check results | `false` |

```yaml
app:
  logs:
    level: debug
    access: true
    model: true
```

## oauth2

Lumen uses [CILogon](https://cilogon.org) for authentication. Register your application at CILogon to get a `client_id` and `client_secret`:

| Field | Description |
|-------|-------------|
| `client_id` | CILogon application ID |
| `client_secret` | CILogon application secret |
| `server_metadata_url` | Well-known URL for the OAuth2 provider (CILogon example: `https://cilogon.org/.well-known/openid-configuration`) |
| `redirect_uri` | Where CILogon sends users after login — matches the callback URL registered at CILogon |
| `scopes` | CILogon scopes to request; `org.cilogon.userinfo` is required for group matching |
| `params` | Optional extra parameters passed to CILogon (e.g. `idphint` to restrict login to a specific identity provider) |
| `allow_unverified_email` | Accept logins whose provider reports the email as unverified (`email_verified: false`). Defaults to `false`. A missing `email_verified` claim is always accepted. |

```yaml
oauth2:
  client_id: cilogon:/client_id/your-id
  client_secret: your-secret
  server_metadata_url: https://cilogon.org/.well-known/openid-configuration
  redirect_uri: https://your-instance/callback
  scopes: openid email profile org.cilogon.userinfo
  allow_unverified_email: false
  params:
    idphint: urn:mace:incommon:uiuc.edu
```

> **Restart required:** Changing any `oauth2` field requires a restart, except `allow_unverified_email`, which is hot-reloaded.

## chat

Controls behavior of the web chat interface:

```yaml
chat:
  remove: hide
  upload:
    max_size_mb: 10
    max_text_chars: 100000
    allowed_extensions:
      - txt
      - md
      - csv
      - json
      - py
      - js
      - ts
      - html
      - css
      - xml
      - yaml
      - yml
      - pdf
      - png
      - jpg
      - jpeg
      - gif
```

| Field | Description |
|-------|-------------|
| `remove` | `hide` = soft-delete (recoverable) / `delete` = hard delete |
| `upload.max_size_mb` | Maximum file upload size in MB |
| `upload.max_text_chars` | Maximum extracted text characters before truncation |
| `upload.allowed_extensions` | Allowed file extensions (uncomment to customize; backend uses magic-byte detection to classify each file) |

## rate_limiting

Controls how many API requests each user can make:

```yaml
rate_limiting:
  # storage_url: redis://localhost:6379/0    # omit = in-memory (single-process)
  limit: "30 per minute"     # per authenticated user identity
```

| Field | Description |
|-------|-------------|
| `storage_url` | Omit for in-memory (dev only); set to a Redis URL for multi-instance deployments |
| `limit` | Rate limit in the format `"N per timeframe"` (e.g. `"30 per minute"`, `"1000 per hour"`) |

> **Restart required:** Changing `storage_url` requires a restart because the Redis client is initialized at startup.

## api

Settings for the OpenAI-compatible API and operational endpoints. **Note:** monitoring and Prometheus
config live **under `api:`** (e.g. `api.monitoring.token`, `api.prometheus.enabled`).

```yaml
api:
  consent: true        # set to false to exempt API requests from the model acknowledgement requirement

  monitoring:
    token: "my-secret-token"

  prometheus:
    enabled: false
    token: ""          # set to a long random string to require Bearer token auth
    multiproc_dir: ""  # path for multi-worker aggregation (e.g. /tmp/prometheus_multiproc)
```

| Field | Description |
|-------|-------------|
| `consent` | When `true` (default), API requests to models with `needs_ack: true` require recorded acknowledgement, just like the web UI. Set to `false` to exempt API requests from the acknowledgement requirement. |

### api.monitoring

A read-only token for uptime monitoring tools (e.g. Uptime Kuma). Pass it as a Bearer token in the
`Authorization` header:

```
Authorization: Bearer my-secret-token
```

The monitoring token can only access `GET /v1/models` and `GET /v1/models/<id>` — it cannot be used to send chat requests or access any other endpoint. Leave `token` empty to disable monitoring access.

### api.prometheus

Optional Prometheus metrics endpoint at `/metrics`:

| Field | Description |
|-------|-------------|
| `enabled` | Enable or disable the metrics endpoint |
| `token` | Bearer token for auth; empty = no auth required |
| `multiproc_dir` | Shared directory for multi-worker aggregation; mount a shared volume here in container deployments |

> **Restart required:** Changing `api.prometheus.enabled` or `api.prometheus.multiproc_dir` requires a restart. `api.prometheus.token` is read on each request and takes effect immediately.

## Environment Variables

Some settings can be controlled via environment variables. The precedence depends on the setting:

All of the following environment variables take precedence over the corresponding `config.yaml` values:

| Environment Variable | Overrides |
|---------------------|-----------|
| `CONFIG_YAML` | Path to the config file (default: `./config.yaml`) |
| `DATABASE_URL` | `app.database.url` |
| `LUMEN_SECRET_KEY` | `app.secret_key` |
| `LUMEN_ENCRYPTION_KEY` | `app.encryption_key` |
| `OAUTH2_CLIENT_ID` | `oauth2.client_id` |
| `OAUTH2_CLIENT_SECRET` | `oauth2.client_secret` |
| `OAUTH2_SERVER_METADATA_URL` | `oauth2.server_metadata_url` |
| `OAUTH2_REDIRECT_URI` | `oauth2.redirect_uri` |
| `OAUTH2_SCOPES` | `oauth2.scopes` |

`app.secret_key` and `app.encryption_key` must be set either in `config.yaml` or via their environment variables — the app will not start without them.

## Security Notes

- **`app.secret_key`** is used for Flask session signing. If leaked, an attacker can forge user sessions. In production, set it to a long random value and inject it via `LUMEN_SECRET_KEY`.
- **`app.encryption_key`** is used to hash API keys stored in the database. Rotating this value invalidates **all** existing user API keys because the hashes can no longer be verified. Use `LUMEN_ENCRYPTION_KEY` to inject it at deploy time without writing it into the config file.
- Never commit `config.yaml` with real secrets to a shared repository. Use `config.yaml.example` as a template and keep your live config file in a private location or inject secrets via environment variables.
