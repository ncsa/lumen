# Changelog

All notable changes to Lumen will be documented in this file.

## [1.7.2] - 2026-05-04

### Changed
- Help docs navigation restructured: `docs/nav.json` is now the single source of truth for page order and slugs
- Help URLs simplified to clean slugs (`/help`, `/help/chat`, `/help/usage`, `/help/api`, `/help/models`, `/help/models/detail`, `/help/clients`, `/help/clients/detail`) — no longer expose filesystem paths
- Relative links and image paths in markdown docs are now resolved against the filesystem and rewritten to canonical `/help/` URLs, fixing broken cross-page links in the rendered help

## [1.7.1] - 2026-05-04

### Added
- Help page is now accessible without logging in; unauthenticated visitors see a Login link in the header instead of Log Out and chat navigation
- Landing (login) page footer now shows Illinois Computes, GitHub Repository, and Request Feature links

### Changed
- Doc image paths converted from absolute (`/help/img/`) to relative (`../img/`)

## [1.7.0] - 2026-05-04

### Added
- Help documentation at `/help` with sidebar navigation and markdown rendering; linked from the utility nav next to Log Out; screenshots in `docs/img/` for chat, usage, models, model detail, and clients pages
- Add Manager dialog now searches users by name or email as you type (up to 10 results), with keyboard-navigable autocomplete dropdown; existing managers are excluded from results
- Test coverage: all source files now have ≥50% coverage (overall up to 75%); new tests for chat routes (conversations, delete, stream validation), metrics endpoint (auth, disabled/enabled), config watcher (`_watcher` reload and `start_config_watcher`), model access control on both `/chat/stream` and `/v1/chat/completions` (blacklist→403, graylist no consent→403, graylist+consent and whitelist pass the gate)
- Client accounts: admins can create client accounts (service entities with no login) and assign managers; managers can create/delete API keys for the client; graylist model consent can be accepted on behalf of a client
- Client list page (`/clients`) shows all clients to admins (with summary stat cards) and only managed clients to regular users; table is sortable and filterable
- Client detail page (`/clients/<id>`) shows usage stat cards (tokens, coins, coin pool, coin refill), managers table, API keys table (sortable/filterable), and a model access table (sortable/filterable) with requests, coins, last used, access status, and model status columns; graylisted models can be clicked to open a consent modal
- `clients:` section in `config.yaml` to configure default and named per-client coin pool limits (`max`, `refresh`, `starting`) and model access default (`whitelist`|`blacklist`); synced to DB on startup and on config hot-reload
- `entity.model_access_default` column (Alembic migration `r8s9t0u1v2w3`) stores per-client model access default; checked in access resolution after group rules and before global default
- Clients nav link hidden from users who manage no clients
- End-to-end tests for client API keys: key created via `POST /clients/<sid>/keys` authenticates against `/v1/` endpoints; soft-deleted key returns 401; no-pool key returns 403
- `app.dev_user` now accepts a dict with `email` and `groups` keys in addition to a plain email string; groups listed under `dev_user.groups` are assigned to the dev user on every `/devlogin`

### Changed
- Usage page now has dedicated API Keys section (sortable/filterable with search) and Model Access section (sortable/filterable with access status, consent buttons, and graylist modal), matching the client detail page layout; web chat stats moved to a standalone table
- New clients no longer automatically add the creating admin as a manager
- Client detail page Model Access table now includes disabled (inactive) and blocked models, hidden by default behind a "Show disabled" toggle
- `entity_model_access.allowed` (bool) replaced with `access_type` (whitelist/blacklist/graylist) to support per-entity graylist overrides; Alembic migration `t0u1v2w3x4y5`
- `clients.model_access.graylist` list in `config.yaml` now syncs to DB as graylist `EntityModelAccess` records, enabling per-client consent-required models alongside a `default: blacklist` policy
- Admin user limits page now supports setting model access overrides to Allowed, Graylist, or Denied
- Coins and costs are now displayed to 2 decimal places (was 4) across all templates

## [1.6.1] - 2026-05-02

### Added
- Test suite expanded to 218 tests: added route tests for `/v1` API auth, `/chat/upload`, and admin group/user management; unit tests for token refill math, metrics middleware (`_normalize_path`, WSGI wrapping), config watcher (`_check_restart_required` restart-required key detection), and health checker (healthy/unhealthy/connection-error/name-fallback per endpoint)
- WCAG 2.1 AA accessibility test suite (`tests/ui/test_accessibility.py`): renders 6 pages via the Flask test client and asserts lang attribute, main landmark, image alt text, form label associations, icon-button aria-label, modal aria-labelledby, data table captions, heading hierarchy, and SkipTo.js presence
- GitHub Actions CI workflow (`.github/workflows/test.yml`): runs `uv run pytest` on every push to `main` and every pull request
- GitHub Actions updated to latest major versions: `actions/checkout` v4→v6, `astral-sh/setup-uv` v5→v8, `actions/setup-python` v5→v6
- `.coverage` and `htmlcov/` added to `.gitignore`
- Test results summary added to GitHub Actions CI using `dorny/test-reporter@v3` (JUnit XML)

### Fixed
- `datetime.utcnow()` replaced with `datetime.now(timezone.utc).replace(tzinfo=None)` across 10 models (column defaults), 4 service/blueprint files, and 2 test files — eliminates Python 3.12+ deprecation warnings
- `Model.query.get(id)` → `db.session.get(Model, id)` and `Model.query.get_or_404(id)` → `db.get_or_404(Model, id)` across all production code and tests — eliminates SQLAlchemy 2.0 legacy API warnings
- Flask-Limiter in-memory storage warning suppressed in tests by adding `rate_limiting.storage_url: "memory://"` to `test_config.yaml`
- `<label>` elements without `for` attributes on Display/Search controls in `groups.html` and `users.html`
- Modal titles changed from `<h5>` to `<h2 class="h5">` across all admin, usage, and clients templates to fix heading hierarchy violations (h1 → h5 skip)
- `lumen/services/health.py`: per-tick check body extracted into `check_all_endpoints()` for testability; `start_health_checker` retains identical loop behaviour

### Changed
- `model_detail` request-count queries ported from raw PostgreSQL SQL (`NOW() - INTERVAL`) to SQLAlchemy ORM (`datetime.utcnow() - timedelta(...)`) for SQLite compatibility
- `/v1/models` request-rate query (`_get_request_rates`) ported from raw PostgreSQL SQL to dialect-agnostic SQLAlchemy Core; same compiled predicate so TimescaleDB chunk pruning is preserved
- `lumen/services/token_refill.py`: per-tick refill body extracted into `refill_coin_balances()` so the math is testable in isolation; `start_coin_refiller` retains identical loop+sleep behavior

## [1.6.0] - 2026-05-02

### Added
- Model detail page at `/models/<name>`: left column shows description and HuggingFace README (fetched server-side, YAML front-matter stripped, rendered as markdown); right sidebar shows availability (status, endpoint health, req/hr, req/24h), model details (context window, max output tokens, modalities, knowledge cutoff, reasoning, function calling), and pricing
- `notice` field on models: optional markdown text shown as a warning callout on the detail page; hidden when unset; configurable via `config.yaml`
- Model names on the `/models` health dashboard are now clickable links to the detail page
- Admin users see each endpoint URL with an up/down badge on the model detail page
- Model access lists: whitelist, blacklist, and graylist support for fine-grained model access control
  - New top-level `model_access:` config section for global defaults with `whitelist`, `blacklist`, `graylist` lists and a `default` field (`whitelist`|`blacklist`|`graylist`, default: `whitelist` = allowed)
  - Per-group `model_access:` section overrides global rules; each group supports `default`, `whitelist`, `blacklist`, `graylist`, and `*` wildcard shorthand
  - Graylisted models appear in the chat model picker with a ⚠ indicator; the user must navigate to the model detail page and click "Acknowledge & Enable Access" once before use
  - Consent is recorded per-user with a timestamp; the model detail page shows the acknowledgment date after consent
  - Access resolution: user admin override > group rules (blacklist > whitelist > graylist) > global rules > effective default
  - New DB migration `q7r8s9t0u1v2` adds `global_model_access`, `entity_model_consents` tables; adds `access_type` column to `group_model_access` (replacing `allowed`); adds `model_access_default` to `groups`

### Changed
- Removed the admin-only chevron toggle and collapsible endpoint rows from the `/models` dashboard (detail page replaces this)
- HuggingFace README: code blocks wrap instead of overflowing (`white-space: pre-wrap`); images capped at 800px wide; inline `font-family` styles suppressed to match the page design system
- Group config: `models: [list]` key is deprecated; use `model_access.whitelist: [list]` instead (warning logged on startup if old key detected)

## [1.5.1] - 2026-05-01

### Fixed
- Chat streaming crash ("Cannot read properties of null") for non-reasoning models that don't emit thinking chunks

### Changed
- Chat model picker now hides models that have no healthy endpoints available

## [1.5.0] - 2026-05-01

### Added
- Chat now streams reasoning model thinking as a collapsible "Thinking…" block above the response; collapses to "Thought" when the answer begins; click to expand and read the full chain-of-thought

### Changed
- Replaced token-based budget with a **coin** system: user balances are now in coins (🪙), deducted by cost (`tokens × coins_per_million / 1M`) rather than raw token count
- Each model's `input_cost_per_million` / `output_cost_per_million` now represents coins per million tokens (set by admin)
- Default new-user pool: 20 coins starting, 0.05 coins/hour refill
- Migration resets all existing user balances to 20 coins; group/entity limits reset to 0 (admin reconfiguration required)
- All cost/budget displays now show 🪙 prefix instead of $, with 4 decimal places for precision
- Raw LLM token counts (input/output) are still tracked and shown in usage tables

### Added
- Admin users page: reset-tokens button to restore a user's token balance to their starting or max limit (whichever is greater)
- Bootstrap Icons for action buttons across the admin UI
- Model config fields: `context_window`, `max_output_tokens`, `supports_reasoning`, `knowledge_cutoff`, `input_modalities`, `output_modalities` — set via config.yaml, synced to DB

### Changed
- Replaced `supports_vision` boolean with `input_modalities` JSON array (e.g. `["text", "image"]`)
- Admin users page: action buttons now use icons (sliders for Limits, play/pause for Activate/Deactivate, refresh for Reset Tokens)
- Admin users page: numeric columns right-aligned, Active column centered
- All prices now display rounded to the nearest penny (2 decimal places) instead of showing fractional cents

### Added
- Chat now supports file attachments: drag-and-drop or click the 📎 button to attach a document or image to any message
- Supported document types (text extracted server-side): `txt`, `md`, `csv`, `json`, `py`, `js`, `ts`, `html`, `css`, `xml`, `yaml`, `yml`, `pdf` (PDF parsed via `pypdf`)
- Supported image types (passed directly to the model as a vision content block): `png`, `jpg`, `jpeg`, `gif`
- File type is validated server-side using magic-byte detection (`filetype` library); a binary file with a mismatched extension is rejected
- Allowed file types and size/text limits are configurable via `chat.upload` in `config.yaml` (`allowed_extensions`, `max_size_mb`, `max_text_chars`); defaults apply if omitted

## [1.4.0] - 2026-03-28

### Added
- Chat responses now stream token-by-token so users see output as it is generated; markdown and math render incrementally during streaming
- Models page: optional `description` and `url` fields per model in `config.yaml`; hovering the model name shows the description as a tooltip, and a link icon (Illinois brand `link` icon) opens the URL in a new tab
- Token balance is now initialized at login so new users see their starting token count on the usage page immediately, rather than after their first API call
- App log level is now configurable via `app.logs.level` in `config.yaml` (default: `INFO`); set to `DEBUG` for verbose output

### Fixed
- Admin analytics heatmap now displays hours in the browser's local timezone instead of UTC

## [1.3.0] - 2026-03-26

### Added
- Footer now includes action links to Illinois Computes, the GitHub repository, and GitHub Issues ("Request Feature"); the GitHub URL defaults to `https://github.com/ncsa/lumen` and can be overridden via `app.github_url` in `config.yaml`
- Prometheus metrics endpoint (`/metrics`) exposing token usage, cost, request counts, latency histograms, endpoint health, and user counts; enable via `prometheus.enabled: true` in `config.yaml`, optionally protected by a Bearer token (`prometheus.token`) and supports multi-worker aggregation via `prometheus.multiproc_dir`
- TimescaleDB `request_logs` hypertable and `request_counts_hourly` continuous aggregate for per-request tracking (model, endpoint, tokens, cost, duration)
- Admin Analytics page (`/admin/analytics`) with period selector, stat cards, user growth charts, token usage, model popularity, and Illinois-branded usage heatmap
- `dev.sh` starts a local TimescaleDB container (`lumen-tsdb`) on port 5678 if not already running

### Changed
- Default `database_url` updated to TimescaleDB on `localhost:5678`; `docker-compose.yml` uses `timescale/timescaledb:latest-pg17`

### Fixed
- Admin users page showed 0 requests/tokens/cost for users who only used the chat interface; stats now read from `model_stats` (covering both chat and API usage) instead of `api_keys` (API-only)

### Changed
- Token budgets are now a single shared pool per user; all model requests draw from one pool, with pool size taken from the largest grant across the user's groups
- Model access is now a separate boolean per user/group, independent of pool size; user-level settings override groups
- Group config format updated: `max`/`refresh`/`starting` keys for pool size, `models: [...]` list for access grants
- Usage page shows token pool as summary cards; Usage by Model lists all accessible models with a Status column and "Show disabled" filter

## [1.2.1] - 2026-03-23

### Fixed
- Chat message timestamps showed "Invalid Date" with PostgreSQL; `isoformat()` returns `+00:00` offset which broke the JS date parser when `"Z"` was appended — switched to `strftime('%Y-%m-%dT%H:%M:%S')` for consistent output across backends

## [1.2.0] - 2026-03-23

### Fixed
- `tools`, `tool_choice`, and all other extra parameters from `/v1/chat/completions` requests are now forwarded to the upstream model; previously they were silently dropped, so tool/function calling never worked through the proxy
- Integer primary key columns created via Alembic migrations were missing PostgreSQL sequences, causing `NotNullViolation` on first insert into `model_stats` (and potentially other tables); migration `g7h8i9j0k1l2` idempotently creates sequences for all affected tables

### Changed
- All foreign keys now have `ON DELETE CASCADE`: deleting an entity removes its API keys, conversations (and messages), limits, balances, stats, and group memberships; deleting a group removes its members and limits; deleting a model config removes its endpoints, per-model limits, balances, and stats

### Added
- LaTeX math rendering in chat responses using KaTeX (via cdnjs); supports `$...$`, `$$...$$`, `\(...\)`, and `\[...\]` delimiters
- `app.dev_user` config option to bypass OAuth for local development; set to an email address to auto-login without OAuth credentials
- Admin Users and Groups are now separate pages in the navbar; each table uses server-side pagination (25/50/100/200 per page) and sorting via AJAX callbacks, supporting up to 40,000+ rows without loading all data upfront
- Users page: stat cards showing total users, requests, tokens, and cost; table includes Tokens Available (∞ for unlimited) and Tokens Used columns; Activate/Deactivate action per user
- Groups page: hides the built-in `default` group; sortable by name, description, members, and active status
- `app.logs.model` config flag (hot-reloadable): when `true`, logs each endpoint health check result at INFO level, showing endpoint up/down and whether the expected model was found
- Model Health Dashboard: admins can expand/collapse per-endpoint detail rows showing endpoint URL, model identifier, last checked time, and up/down status
- Load testing: `math` question type generates random arithmetic expressions (1–3 grouped operations with +, -, *, /) for more realistic prompt variety; configure via `questions` list in `loadtesting/config.yaml`
- LaTeX math rendering in chat responses using KaTeX (self-hosted); supports `$...$`, `$$...$$`, `\(...\)`, and `\[...\]` delimiters
- `app.dev_user` config option to bypass OAuth for local development; set to an email address to auto-login without OAuth credentials
- WCAG 2.1 AA accessibility compliance across all pages
- SkipTo.js v5.10.1 (self-hosted) for landmark/heading skip navigation (WCAG 2.4.1 Bypass Blocks)
- ARIA live region on chat messages area (`role="log"`) so screen readers announce new messages
- Screen-reader-only text for typing indicator ("Assistant is typing")
- Keyboard navigation for conversation sidebar items (Enter/Space to select)
- `aria-label` on all icon-only buttons (hamburger, sidebar toggle, close, remove, info)
- `aria-labelledby` on all modal dialogs; `for`/`id` associations on all form labels and inputs
- `role="img"` with `aria-label` on all emoji used as meaningful content (🔒, ✓, ✗)
- Table `<caption>` elements (visually hidden) on all data tables
- Visually-hidden "Actions" text in empty `<th>` cells
- `aria-selected` and left-border indicator on active conversation item
- Progress bar `aria-label` for token balance display

### Fixed
- Color contrast on assistant chat bubble: darkened from `#e84a27` (3.0:1) to `#b5300c` (5.5:1) (WCAG 1.4.3)
- `.msg-meta` text color darkened from `#6c757d` to `#596068` for contrast on light backgrounds
- Alert auto-dismiss increased from 5s to 20s with pause on hover/focus (WCAG 2.2.1)
- `overflow:hidden` on `<main>` and `.chat-page-layout` changed to `overflow:auto` to prevent clipping at zoom (WCAG 1.4.10)
- Heading hierarchy corrected from h1→h5 to h1→h2 in usage, group detail, and user limits pages
- Conversation remove button now visible on keyboard focus (not just hover)
- Focus ring added for `.btn-outline-primary:focus-visible`
- Logo alt text improved from "I" to "University of Illinois Block I logo"
- Wrong `colspan="6"` fixed to `colspan="7"` on models empty-state row
- `aria-current="page"` added to active admin nav tabs
- Focus management: chat input receives focus after loading a conversation; new-chat button after deletion
- Dynamic status messages in services page now use `role="alert" aria-live="assertive"`

## [1.1.0] - 2026-03-21

### Fixed
- Use `openai.OpenAI` as a context manager in all three call sites (`_do_chat` non-streaming, `_do_chat` streaming, `completions`) so SSL contexts and sockets are always closed after each request, fixing "Too many open files" (EMFILE) under load
- Admin nav link and "Create Service" button were never shown because `is_admin` was not available to templates; now injected via context processor with a live check against `config.yaml` on every request (VULN-06)

### Changed
- All timestamps displayed to users are now shown in their local timezone instead of hardcoded UTC

### Added
- Locust load testing toolkit in `loadtesting/`; `uv run dummy` starts a fake LLM backend, `uv run locust` runs the tests, `setup_users.py` provisions test accounts; `locust` is dev-only and excluded from Docker
- Database connection pool settings now configurable in `config.yaml` under `app.db_pool` (`pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`, `pool_pre_ping`); requires restart to take effect

### Security
- Per-endpoint rate limiting (flask-limiter); single limit configurable under `rate_limiting.limit` in `config.yaml`, keyed by authenticated identity (API key ID or session user ID); returns OpenAI-style JSON 429 for API routes and plain JSON for chat routes
- `secret_key` and `encryption_key` now default to `""` in `config.yaml.example`; the app refuses to start if either is empty, preventing accidental deployment with known default secrets
- API keys are now stored as HMAC-SHA256 hashes in the database; only a short hint (`sk_abcd...xyzw`) is retained for display, so a leaked database backup yields no usable keys
- Disabled user accounts are now immediately blocked from the chat interface; existing sessions are cleared on the next request
- Admin privileges are now re-verified on every request against the current config, so removing an admin from config takes effect immediately without requiring a server restart or logout
- Token refills are now performed exclusively by the background task; removed the on-request lazy refill that could race the background task and grant double tokens

### Changed
- Renamed Python package from `illm` to `lumen` (directory, imports, console script, CSS classes, storage keys)

## [1.0.0] - 2026-03-20

### Added
- Initial production release of **Lumen**, a self-hosted AI chat portal for research institutions
- Web chat interface compatible with OpenAI-compatible endpoints, Ollama, and vLLM
- Federated login via CILogon (institutional identity provider / OAuth2 + OIDC)
- Token budget system — per-user and per-group limits with optional background auto-refresh
- Group management: define groups in `config.yaml`, auto-assign users on login via CILogon attribute rules
- Admin panel for managing users, groups, models, and usage statistics
- Round-robin load balancing across multiple model backends
- Persistent conversation history with optional soft-delete
- Markdown rendering in assistant chat bubbles (XSS-safe)
- Per-model token balance display for users
- API endpoint with usage recording (`/api/...`)
- Model health dashboard with live status and disabled-model indicators
- Hot-reload support for `config.yaml` (app name, tagline, OAuth params, logging settings)
- Illinois Web Toolkit branding (UI colors, Block I logo, `il-blue` palette)
- Docker support with `Dockerfile`, `docker-compose.yml`, and GitHub Actions workflow to publish `ncsa/lumen`
- Configurable app name and tagline via `config.yaml`
- Configurable Werkzeug access log suppression
