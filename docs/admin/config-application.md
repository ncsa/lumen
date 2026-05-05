# Application Settings

These are the core settings that make Lumen work: database, authentication, and general behavior.

## app

| Field | Description |
|-------|-------------|
| `name` | Display name shown in the app header |
| `tagline` | Subtitle shown next to the name |
| `secret_key` | Flask session encryption key (see Security Notes below) |
| `encryption_key` | Used to hash API keys stored in the database. See **Security Notes** |
| `database_url` | PostgreSQL connection string |
| `debug` | Enable debug mode (set to `false` in production) |
| `github_url` | Optional — overrides the default GitHub link in the navbar |

### Database Pool

Optional settings under `app.db_pool`:

| Field | Description | Default |
|-------|-------------|---------|
| `pool_size` | Persistent connections kept open | 5 |
| `max_overflow` | Extra connections allowed above `pool_size` | 10 |
| `pool_timeout` | Seconds to wait before raising a timeout error | 30 |
| `pool_recycle` | Recycle connections after N seconds to avoid stale-connection errors | N/A (no recycling) |
| `pool_pre_ping` | Test each connection before use; silently replace stale ones | `false` |

Example with custom values:

```yaml
app:
  db_pool:
    pool_size: 500
    max_overflow: 50
    pool_timeout: 30
    pool_recycle: 1800
    pool_pre_ping: true
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

The specified email logs in directly without using CILogon OAuth. It also assigns the listed groups to that user. Should not be used in production.

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

```yaml
oauth2:
  client_id: cilogon:/client_id/your-id
  client_secret: your-secret
  server_metadata_url: https://cilogon.org/.well-known/openid-configuration
  redirect_uri: https://your-instance/callback
  scopes: openid email profile org.cilogon.userinfo
  params:
    idphint: urn:mace:incommon:uiuc.edu
```

> **Restart required:** Changing any `oauth2` field requires a restart.

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

## monitoring

A read-only token for uptime monitoring tools (e.g. Uptime Kuma):

```yaml
monitoring:
  token: "my-secret-token"
```

Pass it as a Bearer token in the `Authorization` header:

```
Authorization: Bearer my-secret-token
```

The monitoring token can only access `GET /v1/models` and `GET /v1/models/<id>` — it cannot be used to send chat requests or access any other endpoint. Leave `token` empty to disable monitoring access.

## prometheus

Optional Prometheus metrics endpoint at `/metrics`:

```yaml
prometheus:
  enabled: false
  token: ""          # set to a long random string to require Bearer token auth
  multiproc_dir: ""  # path for multi-worker aggregation (e.g. /tmp/prometheus_multiproc)
```

| Field | Description |
|-------|-------------|
| `enabled` | Enable or disable the metrics endpoint |
| `token` | Bearer token for auth; empty = no auth required |
| `multiproc_dir` | Shared directory for multi-worker aggregation; mount a shared volume here in container deployments |

> **Restart required:** Changing `prometheus.enabled` or `prometheus.multiproc_dir` requires a restart. `prometheus.token` is read on each request and takes effect immediately.

## Environment Variables

Some settings can be controlled via environment variables. The precedence depends on the setting:

**These env vars take precedence over `config.yaml`:**

| Environment Variable | Effect |
|---------------------|--------|
| `CONFIG_YAML` | Path to the config file (default: `./config.yaml`) |
| `DATABASE_URL` | Overrides `app.database_url`; if set, the yaml value is ignored |
| `LUMEN_SECRET_KEY` | Overrides `app.secret_key` (used to sign Flask sessions; leaking it allows session forgery) |
| `LUMEN_ENCRYPTION_KEY` | Overrides `app.encryption_key` for API key hashing |

**These env vars are used as fallbacks when the value is absent from `config.yaml`:**

| Environment Variable | Falls back for |
|---------------------|----------------|
| `OAUTH2_CLIENT_ID` | `oauth2.client_id` |
| `OAUTH2_CLIENT_SECRET` | `oauth2.client_secret` |
| `OAUTH2_SERVER_METADATA_URL` | `oauth2.server_metadata_url` |
| `OAUTH2_REDIRECT_URI` | `oauth2.redirect_uri` |
| `OAUTH2_SCOPES` | `oauth2.scopes` |

`app.secret_key` must be set either in `config.yaml` or via `LUMEN_SECRET_KEY` — the app will not start without it.

## Security Notes

- **`app.secret_key`** is used for Flask session encryption. If this is leaked, an attacker can forge user sessions. In production, set it to a long random value and inject it via the `SECRET_KEY` environment variable.
- **`app.encryption_key`** is used to hash API keys stored in the database. Rotating this value invalidates **all** existing user API keys because the hashes can no longer be verified. Use the `LUMEN_ENCRYPTION_KEY` environment variable to override at runtime without changing the YAML file, or `LUMEN_API_KEY_SECRET` to control the API key hashing specifically.
- Never commit `config.yaml` with real secrets to a shared repository. Use `config.yaml.example` as a template and keep your live config file in a private location or inject secrets via environment variables.
