# Changelog

All notable changes to Lumen will be documented in this file.

## [Unreleased]

### Added
- Footer now includes action links to Illinois Computes, the GitHub repository, and GitHub Issues ("Request Feature"); the GitHub URL defaults to `https://github.com/ncsa/lumen` and can be overridden via `app.github_url` in `config.yaml`
- Prometheus metrics endpoint (`/metrics`) exposing token usage, cost, request counts, latency histograms, endpoint health, and user counts; enable via `prometheus.enabled: true` in `config.yaml`, optionally protected by a Bearer token (`prometheus.token`) and supports multi-worker aggregation via `prometheus.multiproc_dir`

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
