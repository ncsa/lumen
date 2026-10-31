# Lumen Helm Chart

Deploys [Lumen](https://github.com/ncsa/lumen) — an OpenAI-compatible LLM proxy and management platform.

## Prerequisites

- Kubernetes 1.25+
- Helm 3.10+

## Quick Start

```bash
helm install lumen chart/ \
  --set config.secretKey=$(openssl rand -hex 32) \
  --set config.encryptionKey=$(openssl rand -hex 32) \
  --set oauth2.clientId=YOUR_CLIENT_ID \
  --set oauth2.clientSecret=YOUR_CLIENT_SECRET \
  --set ingress.enabled=true \
  --set ingress.host=lumen.example.com
```

## Configuration

### Required Values

| Parameter | Description |
|-----------|-------------|
| `config.secretKey` | Flask session signing key (random string of at least 32 characters; `openssl rand -hex 32` is recommended). Use `existingSecret` instead for production. |
| `config.encryptionKey` | API-key hashing and credential-encryption secret (a different random string of at least 32 characters). |
| `oauth2.clientId` | OIDC client ID |
| `oauth2.clientSecret` | OIDC client secret |

### Using an Existing Secret

Create a secret with the required keys and set `existingSecret`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: lumen-credentials
type: Opaque
stringData:
  secret-key: "<random value of at least 32 characters>"
  encryption-key: "<different random value of at least 32 characters>"
  oauth2-client-id: "..."
  oauth2-client-secret: "..."
  database-url: "postgresql://user:pass@host:5432/lumen"  # optional
```

```yaml
existingSecret: lumen-credentials
postgresql:
  enabled: false
  existingSecret: lumen-credentials
  existingSecretKey: database-url
```

### OAuth2 / OIDC

The chart defaults to [CILogon](https://cilogon.org) but supports any OIDC provider:

```yaml
oauth2:
  serverMetadataUrl: "https://accounts.google.com/.well-known/openid-configuration"
  clientId: "..."
  clientSecret: "..."
```

### PostgreSQL

The bundled PostgreSQL uses `timescale/timescaledb:2.27.2-pg17` to match the production docker-compose setup. Override `postgresql.image` if you need a different version, but keep a TimescaleDB image with a pinned tag: TimescaleDB is required, and on a plain `postgres` image the migrations fail and the pod crash-loops at `flask db upgrade`. See "Upgrading TimescaleDB" in the main README before changing the tag.

**Bundled (default):**
```yaml
postgresql:
  enabled: true
  image: "timescale/timescaledb:2.27.2-pg17"  # override to pin a different version
  auth:
    username: lumen
    password: strong-password
    database: lumen
```

**External:**
```yaml
postgresql:
  enabled: false
  url: "postgresql://lumen:password@my-postgres.example.com:5432/lumen"
```

An external database must also run TimescaleDB (2.27.2 is the tested version): the migrations create the `timescaledb` extension, so the database user needs permission to do that, or a superuser must create it first.

**External via Secret:**
```yaml
postgresql:
  enabled: false
  existingSecret: my-db-secret
  existingSecretKey: database-url
```

### Redis

Redis is optional. Without it, rate limiting is per-process in-memory (fine for single-replica deployments).

**Required for multi-replica deployments:**
```yaml
redis:
  enabled: true
  auth:
    enabled: true
    password: "strong-redis-password"
```

**External:**
```yaml
redis:
  enabled: false
  url: "redis://:password@my-redis.example.com:6379/0"
```

### Ingress

**Standard Kubernetes Ingress:**
```yaml
ingress:
  enabled: true
  className: nginx
  host: lumen.example.com
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  tls:
    - secretName: lumen-tls
      hosts:
        - lumen.example.com
```

**Gateway API (Traefik):**
```yaml
gateway:
  enabled: true
  hostname: lumen.example.com
  parentRef:
    name: traefik-gateway
    namespace: traefik-system
  timeout: "600s"
```

### Group Rules

The chart emits a **version 3** `config.yaml`. Groups — memberships, coin pools, and login auto-join rules — are managed in the app (database), not in config. Auto-join rules are edited on each group's Rules tab; there is no `config.groupRules` value.

### Models

The `models` list is copied verbatim into the `models:` section of the generated `config.yaml`, so it uses the app's config format (snake_case keys, one or more `endpoints` per model; see `config.yaml.example`). The chart does not deploy inference servers — run vLLM, SGLang, or any OpenAI-compatible server separately and point an endpoint at it.

```yaml
models:
  - name: gpt-4o
    input_cost_per_million: 2.5
    output_cost_per_million: 10.0
    context_window: 128000
    endpoints:
      - url: https://api.openai.com/v1
        api_key: "sk-..."
      - url: http://vllm.example.internal:8000/v1   # requests round-robin across healthy endpoints
        api_key: "change-me"
        model: openai/gpt-oss-120b                  # upstream model ID; defaults to name
```

`name`, `input_cost_per_million`, `output_cost_per_million`, and `endpoints` (each with `url` and `api_key`) are required and checked by `values.schema.json`. Removing a model from the list disables it in Lumen rather than deleting it.

## Values Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `replicaCount` | `1` | Number of Lumen pods |
| `wsgiProcesses` | `1` | uvicorn worker processes per pod (wired to `--workers` and `WEB_CONCURRENCY`); >1 requires `api.prometheus.multiprocDir` |
| `wsgiWorkers` | `"auto"` | Concurrent requests per uvicorn worker process (a2wsgi threads; a pod serves `wsgiWorkers` × `wsgiProcesses`); `"auto"` = 2× the per-process `database.poolSize + database.maxOverflow` (auto-sized from `max_connections` when empty), clamped to 10–256; a number sets it directly |
| `wsgiSendTimeout` | `300` | Seconds a worker may block handing one response chunk to the server before the client is treated as gone |
| `startupProbe.periodSeconds` / `failureThreshold` | `5` / `360` | Startup probe budget (30 min by default). Must cover `flask db upgrade` **plus** uvicorn startup — see [Database Migrations](#database-migrations) |
| `startupProbe.initialDelaySeconds` / `timeoutSeconds` | `10` / `5` | Startup probe delay and per-probe timeout |
| `image.repository` | `ncsa/lumen` | Container image repository |
| `image.tag` | `""` | Image tag (defaults to chart appVersion) |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy |
| `migration.backoffLimit` | `6` | Pod retries of the migration Job before it is marked failed; each retry resumes — see [Database Migrations](#database-migrations) |
| `migration.ttlSecondsAfterFinished` | `86400` | Seconds a finished migration Job and its pod logs are kept; `""` keeps it until the next release replaces it |
| `migration.resources` | `{}` | Resources for the migration container; empty uses the app container's `resources` |
| `config.secretKey` | `""` | Flask session signing key |
| `config.encryptionKey` | `""` | API key hashing secret |
| `config.debug` | `false` | Flask debug mode |
| `config.diagnostics` | `true` | Stack-capturing DB/context diagnostics surfaced via `/metrics/debug`; set `false` to remove the per-request capture overhead |
| `config.admins` | `[]` | Admin email addresses |
| `config.configEditor` | `false` | Allow editing config.yaml from the `/admin/config` UI (read-only by default since the chart manages the config) |
| `config.defaults.models.ackMessage` | `""` | Global acknowledgement message for `needsAck` models without their own |
| `config.defaults.tokens.max` / `refresh` / `starting` | `0` | Fallback coin pool for entities with no UI-set limit and no group pool |
| `config.rateLimiting.limit` | `"30 per minute"` | Rate limit per user |
| `config.llm.connectTimeout` | `5` | Seconds to establish the connection to a model backend |
| `config.llm.readTimeout` | `300` | Streaming calls: maximum gap between chunks (not total duration) |
| `config.llm.requestTimeout` | `600` | Non-streaming calls: bound on the whole generation |
| `config.llm.maxRetries` | `1` | Retries for non-streaming upstream calls (streaming calls are never retried) |
| `config.api.keys.ttl` | `"0"` | Maximum and default API key lifetime: `0` (never expire) or e.g. `12h`, `30d`, `8w`; expired keys are revoked |
| `oauth2.serverMetadataUrl` | CILogon OIDC URL | OIDC provider metadata URL |
| `existingSecret` | `""` | Name of pre-existing Secret with credentials |
| `postgresql.enabled` | `true` | Deploy bundled PostgreSQL |
| `postgresql.image` | `timescale/timescaledb:2.27.2-pg17` | PostgreSQL container image |
| `postgresql.url` | `""` | External PostgreSQL URL (when enabled=false); the database must run TimescaleDB |
| `postgresql.existingSecret` | `""` | Secret containing database URL (when enabled=false); the database must run TimescaleDB |
| `redis.enabled` | `false` | Deploy bundled Redis |
| `redis.url` | `""` | External Redis URL (when enabled=false) |
| `redis.existingSecret` | `""` | Secret containing redis URL (when enabled=false) |
| `ingress.enabled` | `false` | Enable standard Ingress |
| `ingress.host` | `""` | Ingress hostname |
| `gateway.enabled` | `false` | Enable Gateway API HTTPRoute |
| `gateway.hostname` | `""` | Gateway hostname |
| `gateway.timeout` | `"600s"` | Request timeout |
| `models` | `[]` | Model definitions copied verbatim into `config.yaml` (see Models section) |
| `serviceMonitor.enabled` | `false` | Create a Prometheus Operator ServiceMonitor scraping `/metrics`. Does **not** carry `api.prometheus.token` as scrape auth — with a token set, scrapes get 401; see the comment in `templates/servicemonitor.yaml` |
| `serviceMonitor.interval` | `"30s"` | ServiceMonitor scrape interval |
| `serviceMonitor.scrapeTimeout` | `"10s"` | ServiceMonitor scrape timeout |

## Database Migrations

Every install, upgrade and rollback creates a Job, `<fullname>-migrate-<revision>`, that runs the image's `upgrade` mode (`flask upgrade`) and exits:

1. all Alembic migrations up to the image's head, including the automatic usage-stats cutover;
2. the post-migration data steps — in 2.3.0, a backfill of every month of `request_logs` that is missing from (or only partly in) the `request_counts_hourly_by_entity` continuous aggregate. On a database where the aggregate is complete this is two indexed probes per month and refreshes nothing.

The Job uses the same image, environment, config Secret and `wait-for-db` initContainer as the app. Its name carries the release revision because a Job cannot be changed once created; Helm deletes the previous revision's Job when it creates the new one.

**Retries resume.** If the Job's pod fails, Kubernetes starts another one, up to `migration.backoffLimit` times. Each attempt skips migrations already applied and probes for missing months again, so it continues where the last one stopped. A finished Job, with its pod logs, stays for `migration.ttlSecondsAfterFinished` seconds (one day by default).

**App pods wait for it.** The Deployment has a second initContainer, `wait-for-migrations`, after `wait-for-db`. It runs the image's `wait-for-migrations` mode, which only reads the database and exits once it is at the migration head this image expects. It has no timeout: a new pod waits as long as the Job takes, then starts. On a `helm upgrade` the old pods keep serving while the Job runs (the default rolling update keeps them until the new pod is Ready). A pod restart with an already migrated database passes the gate immediately. The gate checks the schema only, so new pods can start while the Job is still backfilling; until it finishes, "All Time" usage charts can be incomplete.

**It is a plain Job, not a Helm hook.** A pre-install hook would run before the chart's Secrets and the bundled PostgreSQL exist. As a consequence a failed Job does not fail `helm upgrade`, even with `--wait`: it shows up as a failed Job (`kubectl get jobs`, `kubectl logs job/<fullname>-migrate-<revision>`) and a rollout whose new pod stays in `Init` at the gate, which eventually exceeds the Deployment's `progressDeadlineSeconds`. Fix the cause, then run `helm upgrade` again to get a new Job.

**The chart needs image 2.3.0 or later.** Older images have no `upgrade` or `wait-for-migrations` mode. Until a release sets the chart's `appVersion` to 2.3.0, install from `main` with `image.tag: latest` (or an image built from `main`). Mixed versions fail like this:

- **This chart with an image older than 2.3.0:** the Job fails and the gate initContainer crash-loops, because the image does not know either mode. Nothing new starts; old pods keep serving.
- **An older chart (no Job) with image 2.3.0 or later:** nothing runs the upgrade, so once the image stops migrating on start, pods wait at startup.
- **`helm rollback` to an older image after the database was migrated:** that image does not know the newer revision, so its Job and its gate both fail immediately. Rolling back across a migration needs a database restore.

For now the app container still runs `flask db upgrade` inline before uvicorn starts; with the Job and gate in place that is a no-op, because the database is already at head when the container starts.

## Notes

- **Redis and multi-replica**: With `replicaCount > 1`, configure Redis so rate-limit state is shared across pods.
- **External secrets for Redis**: If using `redis.existingSecret`, also set `redis.url` so it appears in the config. Without the URL, rate limiting falls back to in-memory.
- **Chat upload limits**: The chart has no values for `chat.upload` (`max_size_mb`, `max_text_chars`, `allowed_extensions`); the generated config.yaml omits the section and the application defaults apply (10 MB, 100,000 characters). Set them through `config.extraConfig` if you need different limits.
