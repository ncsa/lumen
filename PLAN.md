# iLLM — Full-Stack Web Application Plan

## Context

Building a greenfield full-stack web app called "iLLM" from scratch in `/Users/kooper/git/ncsa/llm`. The app lets a Demo User interact with an LLM via a chat interface and inspect API key usage statistics. The directory is currently empty.

Key design decisions confirmed:
- **Auth**: OAuth2 (via Authlib); all users can login; email is the unique identifier; disabled users blocked
- **Services**: separate entity (not a user subtype); users can manage N services via a join table; service-scoped usage page shown to relevant users
- **Chat history**: client-side only (localStorage); server never stores message content
- **Usage tracking**: `UserModelStat` running totals per user/model/source; no separate per-request log table
- **Multiple LLM services**: resolved by model name via `ModelConfig` table; each model has multiple endpoints that are round-robined
- **Model config**: loaded from a YAML file (`models.yaml`) at startup; app refuses to start if file is missing or contains no models
- **Health checks**: background thread pings each endpoint every 60s; API calls to a model with no healthy endpoints return 503
- **All keys visible**: soft-deleted keys shown with strikethrough
- **Cost values**: always in **USD** (dollars), not cents

---

## Project Structure

```
/Users/kooper/git/ncsa/llm/
├── run.py
├── config.py
├── models.yaml                    # LLM service configuration (required at startup)
├── requirements.txt
├── .env.example
├── .gitignore
├── migrations/                    # managed by Flask-Migrate / Alembic
├── app/
│   ├── __init__.py                # create_app factory; loads models.yaml; starts health checker
│   ├── extensions.py              # db, migrate singletons
│   ├── models/
│   │   ├── __init__.py
│   │   ├── entity.py              # Entity (user or service, unified table)
│   │   ├── entity_manager.py      # n-m join: user Entity ↔ service Entity
│   │   ├── api_key.py             # APIKey (FK → entity)
│   │   ├── model_config.py        # per-model pricing config
│   │   ├── model_endpoint.py      # per-endpoint URL+key+healthy status
│   │   ├── model_limit.py         # per-entity per-model token budget & hourly refill
│   │   └── model_stat.py          # per-entity per-model per-source running totals
│   ├── blueprints/
│   │   ├── auth/routes.py         # GET /, GET /login (OAuth2 redirect), GET /callback, GET /logout
│   │   ├── chat/routes.py         # GET /chat, POST /chat/send
│   │   ├── models_page/routes.py  # GET /models (model health dashboard)
│   │   ├── services/routes.py     # CRUD for services + user membership
│   │   ├── usage/routes.py        # GET /usage, GET /usage/service/<id>, GET+POST /usage/keys, DELETE /usage/keys/<id>
│   │   └── api/routes.py          # /v1/chat/completions, /v1/completions, /v1/models
│   ├── services/
│   │   ├── llm.py                 # round-robin router over healthy endpoints by model
│   │   ├── health.py              # background thread: pings endpoints every 60s
│   │   └── cost.py                # per-model cost formula
│   ├── templates/
│   │   ├── base.html              # Bootstrap 5 CDN, navbar (Chat, Models, [Services ▾], Usage), Gravatar avatar
│   │   ├── landing.html           # "Login with OAuth2" button, no navbar
│   │   ├── chat.html              # Chat bubbles, model picker dropdown, fixed input bar
│   │   ├── models.html            # Model health table
│   │   ├── services.html          # Services management (create, delete, add/remove members)
│   │   └── usage.html             # API Keys table + modal, Usage by Model table (optionally scoped to a service)
│   └── static/
│       ├── css/app.css            # chat bubbles, avatar, health status badges
│       └── js/app.js              # localStorage chat history, AJAX, modal key gen, soft-delete
```

### `models.yaml` format

```yaml
users:
  tokens:
    maximum: 0    # default max token budget per user (0 = no access until admin grants)
    refresh: 0    # tokens replenished per hour (0 = no auto-refresh)
  admins:
    - admin@example.com   # admin emails (informational for now, not enforced in code yet)

models:
  - name: gpt-4o
    active: true                    # false = model disabled globally
    input_cost_per_million: 5.0     # USD per 1M input tokens
    output_cost_per_million: 15.0   # USD per 1M output tokens
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-abc123
      - url: https://api.openai.com/v1
        api_key: sk-def456          # second key; round-robin between these two

  - name: claude-3-opus-20240229
    active: true
    input_cost_per_million: 15.0
    output_cost_per_million: 75.0
    endpoints:
      - url: https://api.anthropic.com/v1
        api_key: sk-ant-xyz
```

All cost values are in **USD** (dollars). App **refuses to start** if `models.yaml` is missing or contains no active models (validated in `create_app`). The `users.tokens` defaults are applied when a new user logs in for the first time: their `UserModelLimit` rows are created with these defaults, meaning new users have 0 tokens and cannot use any model until an admin grants them a budget.

---

## Database Models

Users and Services are unified in a single `Entity` table, giving every other table a single non-null FK reference.

### `Entity`
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| entity_type | String(8) | `'user'` or `'service'` |
| email | String(256) unique, nullable | users only; primary OAuth2 identity |
| name | String(256) not null | display name (user's full name or service name) |
| initials | String(4) | derived: first letter of first+last name for users, first 2 chars of name for services |
| gravatar_hash | String(64) nullable | MD5(email.strip().lower()); users only |
| active | Boolean | default True; inactive users blocked at login; inactive services blocked from API |
| created_at | DateTime | default utcnow |

Gravatar URL: `https://www.gravatar.com/avatar/{gravatar_hash}?d=404&s=36`. `onerror` JS fallback shows CSS initials avatar.

### `EntityManager` (join table — users who manage services)
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| user_entity_id | FK → entities.id | must be entity_type='user' |
| service_entity_id | FK → entities.id | must be entity_type='service' |

**Unique**: `(user_entity_id, service_entity_id)`. A managing user sees the service in their Services nav dropdown.

### `APIKey`
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| entity_id | FK → entities.id, not null | owner (user or service) |
| name | String(128) | display name |
| key | String(128) unique | `sk_` + secrets.token_urlsafe(32) |
| active | Boolean | False = soft-deleted |
| input_tokens | BigInteger | **running total** |
| output_tokens | BigInteger | **running total** |
| cost | Numeric(12,6) | **running total** cumulative USD |
| last_used_at | DateTime nullable | |
| created_at | DateTime | |

Services can hold API keys for all `/v1/*` operations; the web chat UI is for users only. All keys (active and soft-deleted) shown; deleted rows use strikethrough styling.

### `ModelConfig`
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| model_name | String(128) unique | e.g. "gpt-4o" |
| input_cost_per_million | Numeric(12,6) | USD per 1M input tokens |
| output_cost_per_million | Numeric(12,6) | USD per 1M output tokens |
| active | Boolean | False = model disabled globally |
| created_at | DateTime | |

### `ModelEndpoint`
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| model_config_id | FK → model_configs.id | |
| url | String(256) | base URL e.g. "https://api.openai.com/v1" |
| api_key | String(256) | endpoint-specific API key |
| healthy | Boolean | updated by health checker every 60s |
| last_checked_at | DateTime nullable | timestamp of last health check |
| created_at | DateTime | |

Multiple endpoints per model enable round-robin load distribution and failover. Populated from `models.yaml` on `flask init-db`.

### `ModelLimit`
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| entity_id | FK → entities.id, not null | the user or service being limited |
| model_config_id | FK → model_configs.id, not null | |
| token_limit | BigInteger | -1=no access; -2=unlimited; positive=token budget |
| tokens_per_hour | Integer | tokens replenished per hour; 0 when unlimited |
| tokens_left | BigInteger | current balance; decremented on use; lazy refill on request |
| last_refill_at | DateTime | timestamp of last hourly refill |

**Unique**: `(entity_id, model_config_id)`. Applies identically to users and services. Enforcement in `llm.py`:
- `token_limit == -1` → 403 "No access"
- `token_limit == -2` → skip deduction, allow unlimited
- else → check `tokens_left` before call; deduct after; lazy-refill if `now - last_refill_at >= 1h`

### `ModelStat`
| Field | Type | Notes |
|---|---|---|
| id | Integer PK | |
| entity_id | FK → entities.id, not null | the user or service |
| model_config_id | FK → model_configs.id, not null | |
| source | String(8) | `'chat'` (web UI, users only) or `'api'` (via /v1/* endpoints) |
| requests | Integer | count of requests |
| input_tokens | BigInteger | running total |
| output_tokens | BigInteger | running total |
| cost | Numeric(12,6) | running total |
| last_used_at | DateTime | |

**Unique**: `(entity_id, model_config_id, source)`. Services only ever use source=`'api'`.

**Usage page queries**:
- "Chats" row (Section 1): `SUM` across `ModelStat` where `entity_id=eid AND source='chat'`
- APIKey rows (Section 1): from `APIKey` running totals where `entity_id=eid`
- Usage by Model (Section 2): `GROUP BY model_config_id` for `entity_id=eid`

---

## Routes

| Blueprint | Method | Path | Action |
|---|---|---|---|
| auth | GET | `/` | Landing page with "Login with [Provider]" button |
| auth | GET | `/login` | Redirect to OAuth2 authorization endpoint |
| auth | GET | `/callback` | OAuth2 callback: exchange code, create/update user, set session |
| auth | GET | `/logout` | Clear session → redirect `/` |
| chat | GET | `/chat` | Chat page with model picker (login required) |
| chat | POST | `/chat/send` | AJAX: `{messages, model}` from client, call LLM, update UserModelStat, return reply |
| models_page | GET | `/models` | Model health dashboard (login required) |
| services | GET | `/services` | List user's services + management UI (login required) |
| services | POST | `/services` | Create new service |
| services | DELETE | `/services/<id>` | Mark service inactive |
| services | POST | `/services/<id>/users` | Add user (by email) to service |
| services | DELETE | `/services/<id>/users/<uid>` | Remove user from service |
| usage | GET | `/usage` | Usage page for current user (login required) |
| usage | GET | `/usage/service/<sid>` | Usage page scoped to a specific service (user must manage it) |
| usage | GET | `/usage/keys/generate` | AJAX: return new `sk_` key (not saved yet) |
| usage | POST | `/usage/keys` | Save new APIKey |
| usage | DELETE | `/usage/keys/<id>` | Soft-delete: set is_active=False |
| api | POST | `/v1/chat/completions` | OpenAI-compatible chat completions |
| api | POST | `/v1/completions` | OpenAI-compatible legacy completions |
| api | GET | `/v1/models` | List active models (OpenAI format) |
| api | GET | `/v1/models/<model_id>` | Get specific model (OpenAI format) |

### Services Navigation
If the logged-in user manages at least one active service, a **"Services"** dropdown appears in the navbar between "Models" and "Usage". Each service name in the dropdown links to `/usage/service/<id>`, showing usage stats scoped to that service's API keys and chat usage.

### Models Page (`/models`)

Table columns: Model | Input Cost/1M | Output Cost/1M | Total Instances | Healthy Instances | Last Checked

Each row shows a `ModelConfig` joined with its `ModelEndpoint` list aggregated. Health status per endpoint is shown as a green/red badge. Data fetched from DB (updated by background health checker every 60s).

### Security Model

**Web UI routes** (all `/chat`, `/models`, `/services`, `/usage`, `/usage/*`):
- Protected by `@login_required` decorator: checks `session["entity_id"]`; redirects to `/` if absent
- CSRF: Flask session uses `SECRET_KEY` signed cookies; all state-changing AJAX calls are same-origin (no external CSRF vector)

**OpenAI-compatible API routes** (`/v1/*`):
- Require `Authorization: Bearer sk_...` header on every request
- `@api_key_required` decorator:
  1. Extract token from `Authorization` header; 400 if missing or malformed
  2. Look up `APIKey` by key value; 401 if not found or `active=False`
  3. Look up associated `Entity`; 403 if `entity.active=False`
  4. Check `ModelLimit` for the requested model (`token_limit == -1` → 403; tokens depleted → 429)
  5. Set `g.api_key` and `g.entity` for use in the route handler

**Public routes**: `/` (landing), `/login` (OAuth redirect), `/callback` (OAuth callback)

### OpenAI-Compatible API Endpoints (`app/blueprints/api/`)

**POST `/v1/chat/completions`**
- Accepts: `{"model": "gpt-4o", "messages": [...], "stream": false}`
- Checks `UserModelLimit`; resolves model via `ModelConfig`
- Gets healthy endpoints for model; **if none healthy → 503** "No healthy endpoints for model"
- Round-robins over healthy endpoints; calls LLM
- Updates `UserModelStat` (source=`'api'`) and `APIKey` running totals (if authenticated with an API key)
- Returns standard OpenAI-format JSON; supports `stream: true` via SSE

**POST `/v1/completions`** (legacy)
- Wraps `prompt` into `messages` and delegates to the same LLM router
- Returns OpenAI legacy format

**GET `/v1/models`**
```json
{"object": "list", "data": [{"id": "gpt-4o", "object": "model", "created": ..., "owned_by": "illm"}]}
```

**GET `/v1/models/<model_id>`** — single model object or 404

---

## Key Implementation Details

### Config (`config.py`)
- `DATABASE_URL` controls backend; auto-fix `postgres://` → `postgresql://` in factory
- `MODELS_YAML_PATH` env var: path to `models.yaml` (default: `./models.yaml`)
- `OAUTH2_CLIENT_ID`, `OAUTH2_CLIENT_SECRET`, `OAUTH2_AUTHORIZATION_URL`, `OAUTH2_TOKEN_URL`, `OAUTH2_USERINFO_URL` — OAuth2 provider config
- `OAUTH2_REDIRECT_URI` — callback URL (e.g. `http://localhost:5000/callback`)
- App validates `models.yaml` on startup; raises `SystemExit` with clear error if missing or has no active models

### OAuth2 Auth Flow (via Authlib)

`login_required` decorator checks `session.get("entity_id")`.

**Scopes**: `OAUTH2_SCOPES` env var (default `"openid email profile"`). The `email` claim is required; `name`/`given_name` claims are used for display name and initials; `picture` claim is ignored in favor of Gravatar.

### `flask init-db` — Seed Logic

Syncs `ModelConfig` + `ModelEndpoint` from `models.yaml`. No seed user needed — all users log in via OAuth2. First-time users get `Entity` (type=`'user'`) created with `ModelLimit` rows seeded from `models.yaml` `users.tokens` defaults (maximum=0, refresh=0), blocking access until an admin grants a budget.

### Avatar (Navbar)

Gravatar image with `onerror` JS fallback to CSS initials avatar (36px circle, Bootstrap primary blue, white text).

### Chat Flow (Client-Side History + Model Picker)

1. Page loads: model picker populated server-side; restore `localStorage.getItem("illm_chat")` and render messages
2. User selects model and submits form: append user bubble; push `{role:"user", content}` to in-memory array
3. AJAX POST to `/chat/send`: `{"messages": [{...full history...}], "model": "gpt-4o"}`
4. Backend: checks healthy endpoints for model (503 if none); calls LLM; updates `ModelStat` (source=`'chat'`); returns reply JSON
5. Client: append assistant bubble; push to array; save to `localStorage`

### LLM Service (`app/services/llm.py`) — Round-Robin Router

In-memory round-robin counter per model (thread-safe with a lock). Returns next healthy endpoint by `model_config_id`.

### Health Checker (`app/services/health.py`)

Background daemon thread checks all endpoints every 60s by calling `client.models.list()`. Updates `ModelEndpoint.healthy` and `last_checked_at`.

### Cost Formula (`app/services/cost.py`)

```python
round(input_tokens * input_cost_per_million / 1_000_000 +
      output_tokens * output_cost_per_million / 1_000_000, 6)
```

### API Key Generation Flow
1. Modal `show.bs.modal` → GET `/usage/keys/generate` → `{"key": "sk_" + secrets.token_urlsafe(32)}`
2. JS populates read-only `#key-display` + hidden `#generated-key`
3. Save → POST `/usage/keys` `{name, key}` → DB insert → JS reloads page
4. Dismiss → key discarded (never saved)

---

## `requirements.txt`

```
Flask>=3.0
Flask-SQLAlchemy>=3.1
Flask-Migrate>=4.0
openai>=1.0
python-dotenv>=1.0
psycopg2-binary>=2.9
PyYAML>=6.0
authlib>=1.3
requests>=2.31
```

---

## Startup Sequence

```bash
# 1. Configure models and OAuth2
cp models.yaml.example models.yaml   # edit with LLM endpoints
cp .env.example .env                 # edit with OAuth2 credentials + SECRET_KEY

# 2. Install dependencies
pip install -r requirements.txt

# 3. Initialize database (one-time)
flask db init
flask db migrate -m "initial schema"
flask db upgrade

# 4. Seed model config from YAML
flask init-db    # syncs ModelConfig+ModelEndpoint from models.yaml

# 5. Run
flask run
```

---

## Verification

1. Run without `models.yaml` → app refuses to start with a clear error
2. Visit `http://localhost:5000/` → landing page with "Login" button
3. Click Login → redirected to OAuth2 provider; after auth → `/callback` creates user with 0-token defaults, redirects to `/chat`
4. New user tries to send a message → 403 "No token budget" (tokens.maximum=0 default)
5. (Admin sets tokens via DB or future admin page) → user can now chat
6. Select model in picker, send message → right-aligned bubble; reply left-aligned; history persists on refresh (localStorage)
7. Visit `/models` → model health table; endpoints show green/red health badges
8. Visit `/usage` → "Chats" row with accumulated tokens/cost; Usage by Model breakdown
9. Create a service via `/services` → service appears; add another user by email → both see Services dropdown in nav
10. Click service in Services dropdown → service-scoped usage page
11. Add API key → modal with `sk_...` pre-filled; delete → strikethrough
12. Log Off → session cleared → `/chat` redirects to landing
13. Disable user (set `active=False` in DB) → user's next login returns 403
14. `curl -X POST http://localhost:5000/v1/chat/completions ...` → OpenAI-format JSON
15. `curl http://localhost:5000/v1/models` → list of active models in OpenAI format
16. Kill all endpoints for a model → wait 60s → API call to that model returns 503
