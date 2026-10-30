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
| `config.stats.foldInterval` | `60` | Seconds between passes of the job that folds `request_logs` into the usage totals |
| `config.stats.foldLag` | `300` | Seconds the fold window ends behind now; usage totals lag real time by about `foldLag + foldInterval` |
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

Migrations run automatically at container start: `entrypoint.sh` runs `flask db upgrade` inline and only then `exec`s uvicorn. There is no Helm hook Job. A `busybox` initContainer in the Deployment waits for the database to be reachable first, so the migration does not start against a database that is still coming up.

Two consequences follow from migrating inside the app container rather than in a Job:

- **Migrations overlap with traffic on the old version.** A Job would finish before any new pod started. Here the new pod migrates, and the Deployment uses the default rolling update — at `replicaCount: 1` that is maxSurge 1 / maxUnavailable 0, so the old pod keeps serving the previous code against the half-migrated schema until the new pod is Ready. Where there is no old pod (a fresh install, a pod restart, an eviction, a drained node) nothing serves until `flask db upgrade` returns.
- **The startup probe budget must cover the migration**, not just uvicorn startup — `/healthz` cannot answer until `flask db upgrade` returns. If the migration outlasts `startupProbe.periodSeconds x startupProbe.failureThreshold` (default 30 minutes) the kubelet kills the container mid-migration, the interrupted revision reruns from the beginning on restart, and the pod can end up in `CrashLoopBackOff`. Raise `startupProbe.failureThreshold` before deploying a migration you expect to take longer.

**Upgrading to the stats fold:** after the upgrade that adds `stats_fold_state` has fully rolled out on PostgreSQL, run `flask stats-cutover` once (for example with `kubectl exec` into a Lumen pod). Until then billing keeps updating `model_stats` / `entity_stats` on every request and the fold does nothing; the app logs a warning at startup. `flask check-stats` reports any difference between the folded totals and `request_logs`, and `--fix` repairs it.

A migration that *fails* exits non-zero and the container dies immediately (`entrypoint.sh` runs under `set -e`), so a broken migration surfaces as a crashing pod rather than a blocked rollout. For a migration that is known to be long, run `flask db upgrade` manually against the database before the deploy.

## Notes

- **Redis and multi-replica**: With `replicaCount > 1`, configure Redis so rate-limit state is shared across pods.
- **External secrets for Redis**: If using `redis.existingSecret`, also set `redis.url` so it appears in the config. Without the URL, rate limiting falls back to in-memory.
- **Chat upload limits**: The chart has no values for `chat.upload` (`max_size_mb`, `max_text_chars`, `allowed_extensions`); the generated config.yaml omits the section and the application defaults apply (10 MB, 100,000 characters). Set them through `config.extraConfig` if you need different limits.
