# iLLM Code Review Report

| | |
|---|---|
| **Date** | 2026-03-18 |
| **Reviewer** | Senior Software Developer |
| **Project** | iLLM — Integrated LLM Access Manager |
| **Scope** | Full codebase: security, code quality, and architecture |

---

## Executive Summary

The iLLM project is a well-structured Flask application for managing LLM access with OAuth2 authentication, cost tracking, and multi-tenant support. The overall architecture is sound and the separation of concerns is clear. Three high-severity security issues were identified that must be addressed before any public or production deployment. An additional five medium-severity and ten low-severity findings are documented below.

---

## 1. Security Findings

### 1.1 HIGH — API Keys Stored in Plaintext

**Files:** `app/models/api_key.py`, `app/models/model_endpoint.py`

User-generated `sk_*` keys and LLM endpoint `api_key` values are stored in the database as plaintext strings. A database breach immediately exposes every key — both user keys and upstream provider credentials.

**Recommendation:** Store a one-way hash (e.g., SHA-256) of user API keys for lookup, and show the full key only once at generation time. LLM endpoint credentials should be encrypted at rest (e.g., using Fernet) or stored in a secrets manager and referenced by ID.

---

### 1.2 HIGH — Hardcoded Secret Key Fallback

**File:** `config.py`

```python
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
```

Flask uses `SECRET_KEY` to sign sessions. If the environment variable is not set, the application silently falls back to a known default value, allowing any attacker to forge valid session cookies.

**Recommendation:** Remove the fallback entirely. Raise a `RuntimeError` at startup if `SECRET_KEY` is absent or if its value matches a known weak default.

---

### 1.3 HIGH — No CSRF Protection on State-Mutating Endpoints

**Files:** `app/blueprints/services/routes.py`, `app/blueprints/usage/routes.py`, and other form-accepting routes

Routes such as `POST /services`, `POST /usage/keys`, and `DELETE /services/<id>` accept form submissions without any CSRF token validation. The `@login_required` decorator only confirms a session exists — it does not verify request origin.

**Recommendation:** Integrate Flask-WTF and enforce CSRF tokens on all state-mutating endpoints, or implement the synchronizer token pattern manually.

---

### 1.4 MEDIUM — Admin Decorator Does Not Verify Login First

**File:** `app/decorators.py`

The `@admin_required` decorator checks `session.get("is_admin")` without first verifying that `session["entity_id"]` is present. If session signing were ever compromised, a crafted session with only `is_admin=True` set could bypass the check.

**Recommendation:** Compose `@login_required` inside `@admin_required`, or explicitly assert that `entity_id` is present before checking the admin flag.

---

### 1.5 MEDIUM — Gravatar Exposes Reversible Email Hashes

**File:** `app/models/entity.py`

MD5 hashes of user email addresses are stored in the database and served to clients as Gravatar identifiers. MD5 hashes of common or known email addresses are trivially reversible via rainbow tables or brute-force enumeration.

**Recommendation:** Generate server-side initials-based avatars without exposing the hash to clients, or proxy Gravatar requests server-side so the hash is never transmitted to the browser.

---

### 1.6 MEDIUM — No Rate Limiting on Any Endpoint

**Files:** `app/blueprints/api/routes.py`, `app/blueprints/chat/routes.py`

Neither the web chat interface nor the OpenAI-compatible API endpoints implement request rate limiting beyond the token budget. An attacker with a valid key can make a high volume of small requests (each within budget) to enumerate, abuse, or degrade service.

**Recommendation:** Add Flask-Limiter with per-IP and per-key limits on authentication and LLM proxy endpoints. Authentication endpoints (login, callback) are especially sensitive.

---

### 1.7 MEDIUM — LLM Endpoint Credentials Visible in Database

**File:** `app/models/model_endpoint.py`

The `api_key` column on `ModelEndpoint` is a plain `String`. Any user with direct database access, or any code path that enables SQLAlchemy query logging, can read upstream provider API keys in plaintext.

**Recommendation:** Encrypt this field at rest using a key stored outside the database (e.g., Fernet with an app-level encryption key), or store credentials in a dedicated secrets manager and reference them by identifier.

---

### 1.8 MEDIUM — No Session Expiry Configured

**File:** `config.py`

`PERMANENT_SESSION_LIFETIME` is not set anywhere in the configuration. Flask sessions persist until the browser discards them, leaving sessions open indefinitely and extending the window for session hijacking.

**Recommendation:** Set an explicit lifetime in `config.py`:

```python
from datetime import timedelta
PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
```

---

### 1.9 LOW — Floating-Point Arithmetic Used for Billing

**File:** `app/services/cost.py`

```python
return round((input_tokens * input_rate / 1_000_000) + ..., 6)
```

Cost calculations use Python `float`, which is subject to IEEE 754 rounding errors. These errors are small per call but can accumulate over millions of requests and introduce billing discrepancies.

**Recommendation:** Use Python's `decimal.Decimal` type throughout all cost calculations. The database column is already `Numeric(12, 6)`, which supports the required precision.

---

### 1.10 LOW — OAuth2 Redirect URI Not Asserted at Application Layer

**File:** `app/blueprints/auth/routes.py`

The OAuth2 callback route relies entirely on Authlib's internal validation of the redirect URI. No application-layer assertion exists to confirm the callback URL matches `OAUTH2_REDIRECT_URI`.

**Recommendation:** Add an explicit assertion or a logged warning if the received callback URL does not match the configured value. This catches misconfiguration early and makes intent explicit.

---

## 2. Code Quality Findings

### 2.1 Silent Exception Swallowing in Health Checker

**File:** `app/services/health.py`

```python
except Exception:
    pass
```

All errors during endpoint health checks are silently discarded. Persistent failures — such as network outages, invalid credentials, or upstream API changes — will never surface to operators.

**Recommendation:** Log all exceptions at `WARNING` level, including the endpoint URL and error message, so that health check failures are observable.

---

### 2.2 No Timeout on LLM Calls

**File:** `app/services/llm.py`

The OpenAI SDK client is initialized without a `timeout` parameter. A hung or slow upstream endpoint holds a WSGI thread open indefinitely and can exhaust the worker pool under load.

**Recommendation:** Configure an explicit timeout on the SDK client:

```python
client = OpenAI(api_key=..., timeout=httpx.Timeout(connect=5.0, read=120.0))
```

---

### 2.3 Magic Number for Health Check Interval

**File:** `app/services/health.py`

```python
time.sleep(60)
```

The 60-second interval is hardcoded with no explanation or configuration path.

**Recommendation:** Define `HEALTH_CHECK_INTERVAL_SECONDS = 60` in `config.py` and reference it from the health service.

---

### 2.4 No Schema Validation for `models.yaml`

**File:** `app/__init__.py`

The configuration file is parsed with `yaml.safe_load()` but no validation of required fields, types, or allowed values is performed. A misconfigured file produces obscure `KeyError` or `AttributeError` exceptions at startup rather than actionable error messages.

**Recommendation:** Validate the parsed dictionary against a schema at startup — using Pydantic, `cerberus`, or a simple hand-written validator — and print a clear error message indicating the problematic field before exiting.

---

### 2.5 No Input Validation on JSON Request Bodies

**Files:** `app/blueprints/api/routes.py`, `app/blueprints/chat/routes.py`

Routes accept `request.get_json()` and access fields by key without schema validation. Missing required fields or unexpected types produce unhandled exceptions that leak internal tracebacks.

**Recommendation:** Use Pydantic models or marshmallow schemas to validate and deserialize all incoming JSON before it is processed by business logic.

---

### 2.6 Token Refill Uses Integer Truncation

**File:** `app/services/llm.py`

```python
int(hours_elapsed) * tokens_per_hour
```

`int()` truncates toward zero, so 2.9 elapsed hours only delivers 2× the hourly token allowance. Users consistently lose a fraction of their entitled budget on every refill cycle.

**Recommendation:** Use `math.floor()` with tracked remainder, or accumulate a fractional token balance, to ensure users receive their full entitled tokens over time.

---

### 2.7 Potential N+1 Query on Services Page

**File:** `app/blueprints/services/routes.py`

Services are loaded in a single query, but related members are accessed via lazy-loaded relationships in the template. This may trigger one additional query per service to load member data.

**Recommendation:** Use `joinedload` or `selectinload` on the `EntityManager` relationship when fetching services for the management page.

---

### 2.8 Entity Active Check Outside the Auth Decorator

**File:** `app/blueprints/api/routes.py`

The `@api_key_required` decorator validates that the API key is active, but the check on `entity.active` appears to occur after the decorator returns, inside the route handler. This means the active check is not consistently enforced and could be missed in future routes.

**Recommendation:** Move the entity active check into the `@api_key_required` decorator so it is enforced uniformly across all protected routes.

---

## 3. Architecture Observations

The following are not defects but warrant consideration as the project scales.

**Client-side chat history (`app/templates/chat.html`):** Storing conversation history in `localStorage` is a deliberate privacy-preserving design choice. The trade-off — no server-side history, no multi-device continuity, and loss on cache clear — should be documented explicitly for users.

**Health checker under multi-worker WSGI:** The background daemon thread works correctly for single-process deployments. Under a multi-worker server (e.g., Gunicorn with `--workers 4`), each worker spawns its own health checker, resulting in `N` concurrent threads hitting every endpoint on the same interval. Consider a distributed lock, a Redis-backed scheduler, or a dedicated external health check process.

**Token budget with special sentinel values:** The `-1` (blocked) and `-2` (unlimited) sentinel values embedded in an integer column are not self-documenting. A future developer reading a query result of `-2` will not immediately understand its meaning. Consider a separate `policy` enum column or well-named constants at the model layer.

---

## 4. Summary

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1.1 | HIGH | API keys stored in plaintext | `models/api_key.py`, `models/model_endpoint.py` |
| 1.2 | HIGH | Hardcoded secret key fallback | `config.py` |
| 1.3 | HIGH | No CSRF protection | Multiple blueprint routes |
| 1.4 | MEDIUM | Admin decorator bypasses login check | `decorators.py` |
| 1.5 | MEDIUM | Gravatar exposes reversible email hashes | `models/entity.py` |
| 1.6 | MEDIUM | No rate limiting | `blueprints/api/`, `blueprints/chat/` |
| 1.7 | MEDIUM | LLM endpoint credentials visible in DB | `models/model_endpoint.py` |
| 1.8 | MEDIUM | No session expiry configured | `config.py` |
| 1.9 | LOW | Floating-point arithmetic for billing | `services/cost.py` |
| 1.10 | LOW | OAuth2 redirect URI not asserted | `blueprints/auth/routes.py` |
| 2.1 | LOW | Silent exception swallowing | `services/health.py` |
| 2.2 | LOW | No timeout on LLM calls | `services/llm.py` |
| 2.3 | LOW | Magic number for health check interval | `services/health.py` |
| 2.4 | LOW | No schema validation for models.yaml | `app/__init__.py` |
| 2.5 | LOW | No input validation on JSON bodies | `blueprints/api/`, `blueprints/chat/` |
| 2.6 | LOW | Token refill uses integer truncation | `services/llm.py` |
| 2.7 | LOW | Potential N+1 query on services page | `blueprints/services/routes.py` |
| 2.8 | LOW | Entity active check outside auth decorator | `blueprints/api/routes.py` |

**Priority recommendation:** Resolve findings 1.1, 1.2, and 1.3 before any production or public deployment. Address the MEDIUM findings before exposing the service to untrusted users. The LOW findings can be tracked as ongoing hardening work.
