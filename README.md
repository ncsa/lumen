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
  secret_key: change-me-to-something-random   # any long random string
  database_url: sqlite:///lumen.db            # or a postgres:// URL
  debug: false
```

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
