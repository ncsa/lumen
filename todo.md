# Technical Debt / Follow-up Tasks

## [PERF] Bulk access resolution for get_model_access_status

**Files:** `lumen/services/llm.py`, `lumen/blueprints/models_page/routes.py`, `lumen/blueprints/chat/routes.py`

**Problem:** `get_model_access_status(entity_id, model_config_id)` is called in a loop
over every active model on both the `/models` page (`models_page/routes.py:21`) and
the `/chat` page (`chat/routes.py:78`). Each call fires up to 4 DB queries:
   1. `EntityModelAccess` user-level lookup (short-circuits if found)
   2. `_get_active_group_ids(entity_id)` — a GROUP JOIN (same result every iteration, N calls)
   3. `GroupModelAccess` per-model rule lookup
   4. `Group.model_access_default` lookup

**Impact:**
- 10 active models, group member, no user-level rules → ~40 queries per page load
- 20 active models → ~80 queries per page load
- Both `/models` and `/chat` (the main page) are affected on every request

**Root cause:** `_get_active_group_ids(entity_id)` runs the same JOIN query N times
because `group_ids` is not shared across loop iterations.

**Fix approach (option A — low blast radius):** Compute `group_ids` once before the
loop in each route, and add an optional `group_ids` parameter to
`get_model_access_status` so it can skip the `_get_active_group_ids` call when
pre-computed. Keeps the public interface backward-compatible.

**Fix approach (option B — full optimization):** Replace per-model loop with a single
bulk SQL query that resolves access for all (entity, model) pairs at once using LEFT
JOINs across `entity_model_access`, `group_member`, `group_model_access`, and `group`.
Returns a dict `{model_config_id: status}` in one round-trip. More invasive but
eliminates the N+1 entirely.

**Risk:** Access control logic is security-sensitive; any regression silently
over/under-grants access. Needs thorough test coverage before merge.

---

## [SECURITY] time_bucket f-string in admin SQL (low priority)

**File:** `lumen/blueprints/admin/routes.py` (lines ~348, 355, 372, 380)

**Problem:** The `bucket` variable is interpolated into raw SQL via an f-string:
```python
db.session.execute(text(f"SELECT time_bucket('{bucket}', bucket) ..."))
```

**Current safety:** `bucket` is sourced exclusively from the hardcoded `_PERIODS` dict
at the top of the file — no user input reaches it. Not currently exploitable.

**Risk if changed:** If anyone adds a route that passes user-controlled input to
`_period_bucket()`, this becomes a SQL injection vector.

**Fix:** Replace the f-string with a SQLAlchemy `bindparams` for the interval literal,
or add an assertion `assert bucket in _PERIODS.values()` as an explicit guard.
Note: PostgreSQL's `time_bucket` accepts a bind parameter for the interval, e.g.
`time_bucket(:interval_val, bucket)` with `.bindparams(interval_val='1 hour')`.

---

## [SECURITY] Missing input validation and sanitization

**File:** `lumen/blueprints/chat/routes.py` (various locations)

**Problem:** Several endpoints lack proper input validation:
- File upload endpoint (`/chat/upload`) validates extension but could benefit from 
  additional MIME type validation
- Text truncation uses user-controlled `max_chars` without upper bounds checking
- Multiple JSON endpoints trust client-sent data structure

**Risk:** While current implementation has some guards, missing validation could 
lead to resource exhaustion or bypassed security controls if protections change.

**Fix:** Implement comprehensive input validation including:
- Upper bounds on all user-controllable limits
- Strict schema validation for JSON inputs
- Defense-in-depth validation for file uploads (extension + content-type + magic bytes)

---
## [MAINTENANCE] Inconsistent test coverage

**Observation:** The project has unit tests in `tests/unit/` and UI tests in `tests/ui/` 
but coverage appears inconsistent across modules.

**Risk:** Critical paths like authentication, authorization, and payment processing 
may lack adequate test coverage, increasing regression risk.

**Recommendation:** 
- Conduct coverage analysis to identify undertested areas
- Prioritize adding tests for security-critical functions
- Maintain existing test patterns when adding new features

---
