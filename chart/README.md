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
| `config.secretKey` | Flask session signing key (random 32-byte hex). Use `existingSecret` instead for production. |
| `config.encryptionKey` | API key hashing secret (different from secretKey). |
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
  secret-key: "..."
  encryption-key: "..."
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
  # redirectUri is auto-computed from ingress.host or gateway.hostname
  # Override explicitly if needed:
  redirectUri: "https://lumen.example.com/callback"
```

### PostgreSQL

The bundled PostgreSQL uses `timescale/timescaledb:2.26.4-pg17` to match the production docker-compose setup. Override `postgresql.image` if you need a different version.

**Bundled (default):**
```yaml
postgresql:
  enabled: true
  image: "timescale/timescaledb:2.26.4-pg17"  # override to pin a different version
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

### Models

Models are registered in Lumen's config regardless of `replicas`. Use `replicas: 0` for external endpoints, `replicas: 1+` to deploy an inference server in-cluster.

When `replicas > 0`, two fields are always required:

| Field | Purpose |
|-------|---------|
| `image` | Container image with an explicit, pinned tag (e.g. `vllm/vllm-openai:v0.9.1`). Never use `latest` — vLLM and SGLang releases frequently change CLI flags and behaviour. |
| `engine` | `vllm` or `sglang`. Determines the launch command and health probe path — vLLM uses `/health` for all probes; SGLang uses `/health` for liveness but `/health_generate` for readiness. The image alone is not enough to infer this. |

#### External model (replicas=0)

```yaml
models:
  - name: gpt-4o
    replicas: 0
    model: gpt-4o
    url: https://api.openai.com/v1
    apiKey: "sk-..."
    lumen:
      inputCostPerMillion: 2.5
      outputCostPerMillion: 10.0
      contextWindow: 128000
      active: true
```

#### In-cluster vLLM deployment

```yaml
models:
  - name: llama3-8b
    replicas: 1
    engine: vllm
    image: "vllm/vllm-openai:v0.9.1"
    model: meta-llama/Meta-Llama-3-8B-Instruct
    apiKey: "change-me"
    port: 8000
    gpu:
      enabled: true
      count: 1
      runtimeClassName: nvidia
      tolerations:
        - key: dedicated
          operator: Equal
          value: gpu
          effect: NoSchedule
    resources:
      limits:
        cpu: "8"
        memory: 80Gi
        nvidia.com/gpu: 1
      requests:
        cpu: "4"
        memory: 16Gi
        nvidia.com/gpu: 1
    storage:
      enabled: true
      size: 100Gi
      storageClassName: longhorn
      mountPath: /mnt/pvc
    shmSize: 10Gi
    extraArgs:
      - "--max-model-len=8192"
      - "--gpu-memory-utilization=0.9"
    hfToken:
      secretName: hf-token-secret
      secretKey: HF_TOKEN
    healthCheck:
      startupInitialDelay: 30
      startupPeriod: 30
      startupFailureThreshold: 120   # 60 min budget for large model loads
    lumen:
      inputCostPerMillion: 0.1
      outputCostPerMillion: 0.3
      contextWindow: 8192
      active: true
```

#### In-cluster SGLang deployment

```yaml
models:
  - name: qwen3-8b
    replicas: 1
    engine: sglang
    image: "lmsysorg/sglang:v0.5.11-cu129-runtime"
    model: Qwen/Qwen3-8B
    apiKey: "change-me"
    port: 8000
    gpu:
      enabled: true
      count: 1
      runtimeClassName: nvidia
    resources:
      limits:
        cpu: "8"
        memory: 80Gi
        nvidia.com/gpu: 1
      requests:
        cpu: "4"
        memory: 16Gi
        nvidia.com/gpu: 1
    storage:
      enabled: true
      size: 50Gi
      storageClassName: longhorn
    shmSize: 10Gi
    extraArgs:
      - "--reasoning-parser=qwen3"
      - "--tool-call-parser=qwen3_coder"
      - "--mem-fraction-static=0.8"
    lumen:
      inputCostPerMillion: 0.05
      outputCostPerMillion: 0.10
      contextWindow: 32768
      active: true
```

## Values Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `replicaCount` | `1` | Number of Lumen pods |
| `image.repository` | `ghcr.io/ncsa/lumen` | Container image repository |
| `image.tag` | `""` | Image tag (defaults to chart appVersion) |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy |
| `config.secretKey` | `""` | Flask session signing key |
| `config.encryptionKey` | `""` | API key hashing secret |
| `config.debug` | `false` | Flask debug mode |
| `config.admins` | `[]` | Admin email addresses |
| `config.rateLimiting.limit` | `"30 per minute"` | Rate limit per user |
| `oauth2.serverMetadataUrl` | CILogon OIDC URL | OIDC provider metadata URL |
| `oauth2.redirectUri` | `""` | Auto-computed from ingress/gateway if empty |
| `existingSecret` | `""` | Name of pre-existing Secret with credentials |
| `postgresql.enabled` | `true` | Deploy bundled PostgreSQL |
| `postgresql.image` | `timescale/timescaledb:2.26.4-pg17` | PostgreSQL container image |
| `postgresql.url` | `""` | External PostgreSQL URL (when enabled=false) |
| `postgresql.existingSecret` | `""` | Secret containing database URL (when enabled=false) |
| `redis.enabled` | `false` | Deploy bundled Redis |
| `redis.url` | `""` | External Redis URL (when enabled=false) |
| `redis.existingSecret` | `""` | Secret containing redis URL (when enabled=false) |
| `ingress.enabled` | `false` | Enable standard Ingress |
| `ingress.host` | `""` | Ingress hostname |
| `gateway.enabled` | `false` | Enable Gateway API HTTPRoute |
| `gateway.hostname` | `""` | Gateway hostname |
| `gateway.timeout` | `"600s"` | Request timeout |
| `models` | `[]` | Model definitions (see Models section) |

## Database Migrations

Migrations run automatically as a Helm pre-install/pre-upgrade Job (`flask db upgrade`). The job runs `busybox` to wait for the database before migrating.

## Notes

- **Redis and multi-replica**: With `replicaCount > 1`, configure Redis so rate-limit state is shared across pods.
- **GPU models**: Ensure your cluster has GPU nodes with the `nvidia` RuntimeClass and the NVIDIA device plugin installed.
- **Model PVCs**: Chart-managed PVCs use `ReadWriteMany` access mode. Ensure your storage class supports it (e.g., Longhorn, NFS).
- **External secrets for Redis**: If using `redis.existingSecret`, also set `redis.url` so it appears in the config. Without the URL, rate limiting falls back to in-memory.
