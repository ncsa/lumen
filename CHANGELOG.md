# Changelog

All notable changes to Lumen will be documented in this file.

## [Unreleased]

### Added

- Users can create and manage groups, members, and model grants through the new Groups pages; admins manage group coin policies. (PR #41)
- Admins manage database-backed auto-join rules per group, with matching login claims keeping memberships synchronized. (PR #41)
- Models support configurable early-access acknowledgements and badges. (PR #41)
- Models support end dates that hide and disable expired entries. (PR #41)
- Model detail pages show when a model was first seen. (PR #41)

### Security

- Upstream endpoint API keys are encrypted at rest with `app.encryption_key`. (PR #41)
- Model endpoint probes reject unsafe or internal targets, redirects, and DNS rebinding. (PR #41)
- Request bodies are capped before oversized uploads are buffered. (PR #41)
- `/metrics/debug` supports a separate bearer token via `api.prometheus.debug_token`. (PR #41)
- The config editor rejects structurally invalid version 3 configurations before saving. (PR #41)

### Changed

- Database and app-context diagnostics can be disabled with `app.diagnostics: false`. (PR #41)
- The implicit `default` group is removed; users without a group pool continue to use `defaults.tokens`. (PR #41)
- Project ownership transfers use a Change Owner dialog and require the new owner to already be a manager. (PR #41)
- **Breaking:** Config version 3 removes user, project, client, group, and auto-join sections from config and Helm values; migrate old values files. (PR #41)
- **Breaking:** Model access is database-managed: unowned models are public; owned models are limited to their owner and granted groups. Back up the database and assign restricted-model owners when upgrading. (PR #41)
- **Breaking:** Project settings and per-user coin pools move from config to database-backed UI, preserving existing database values. (PR #41)
- Project detail pages use the profile-card and tabbed layout. (PR #41)
- Native browser dialogs are replaced with accessible application modals. (PR #41)
- Projects appear between Profile and Models in navigation. (PR #41)
- Project and admin-user tables expose usage and coin columns, newest-first last-use sorting, and inline admin actions. (PR #41)
- Project and user settings open from inline edit buttons with role-aware access. (PR #41)
- Deactivated projects remain visible to their administrators and managers. (PR #41)
- Profile and project pages show the next refill time. (PR #41)

### Fixed

- Blocked, disabled, and expired models no longer expose metadata through detail, profile, or project pages. (PR #41)
- Non-serving CLI commands no longer start background workers during migrations and other maintenance commands. (PR #41)
- Data tables consistently render cell borders. (PR #41)
- Models.dev knowledge cutoffs are normalized to `YYYY-MM`. (PR #41)
- Config-editor saves tolerate unwritable backup paths. (PR #41)
- Gravatar hashing works on FIPS-enabled Python builds. (PR #41)

## [1.26.0] - 2026-08-22

### Fixed

- Load testing now serves through the real ASGI bridge (asgi:app) instead of a bare Flask app, so queue-wait/preflight/abort metrics are measured and `LUMEN_WSGI_WORKERS` is honored (#47).
- Load-test accounts are created with `access_type: "allowed"` instead of the legacy unrecognised `"whitelist"` (which denied every model).
- A missing T0 bridge mark is now loudly logged (and a 500 under `LUMEN_REQUIRE_BRIDGE=1`) instead of silently skipped.
- Every new PostgreSQL session is pinned to UTC (`SET TIME ZONE 'UTC'` on connect), fixing `/usage` heatmap and bucket errors on non-UTC servers.
- A client disconnecting in the milliseconds between billing and the final stream yield no longer loses the reply it paid for.
- `/chat/stream` closes the LLM stream in a `finally` instead of relying on garbage collection to bill abandoned streams.
- Billing failures on the streaming API are no longer misreported as upstream failures.
- Bumped dependency floors to the majors actually locked/tested (`openai>=3`, `flask-limiter>=4`, `pypdf>=6`, `psutil>=7`, `pytest>=9`, `pytest-cov>=7`).
- Client disconnects are now detected in production — the ASGI bridge delivers `http.disconnect` and both streaming paths stop/close on it.
- All backend calls are timeout/retry-bounded, configurable under `llm:`.
- A half-open TCP link no longer pins a worker thread; sends are bounded by `LUMEN_WSGI_SEND_TIMEOUT`.
- Abandoned streams are now billed for what they consumed (exact usage, else an estimate) instead of zero cost.
- The test suite no longer runs against the dev database; it requires SQLite at exactly `test_lumen.db`.
- PostgreSQL integration tests use per-module private migrations instead of sharing (and clobbering) one DB.
- The aware-`last_refill_at` test flake is fixed and the branch is actually reached.
- Every span in the LLM paths now uses one monotonic clock (`tests/unit/test_single_clock.py`).
- `/usage` no longer drops the first partial hour of each window.
- Deleted entities no longer reappear in `/usage` after a continuous-aggregate refresh.
- Org-wide `/usage` charts no longer lag up to two hours (continuous aggregate built real-time, `materialized_only=false`).
- Fixed two flaky pool-registry tests and the metrics-snapshot race.
- Helm-deployed config no longer warns about its own `config_editor`/`email_themes` keys.
- `app.encryption_key` and `app.logs.level` are now flagged restart-required.
- Chart README no longer documents a migration Job that does not exist (migrations run inline in the entrypoint).
- Startup-probe budget raised to 30 minutes (configurable) so a migration can complete.
- Chart README/values fixes (image tag, `wsgiSendTimeout`, `config.llm.*`, `startupProbe`, ServiceMonitor auth note, chat.upload note).
- `config.yaml.example` now documents `app.config_editor`, `app.logs`, `app.github_url`, `app.email_themes`.
- Per-entity `/usage` fallback reads the aggregate's materialised hypertable, not raw rows below the watermark.
- Live-request ticket is released before (not after) closing the stream on `/chat/stream`.
- WSGI worker-pool depth counters no longer under-count cancelled ASGI tasks.
- Chart only mounts `PROMETHEUS_MULTIPROC_DIR` when Prometheus is actually enabled.
- Rejection-path metrics no longer force a fresh middleware import.
- Aware `last_refill_at` is normalised to UTC instead of silently dropping its offset.
- The single-clock guard now covers the wsgi_disconnect timing spans.
- `RequestLog` model now declares the composite index its PostgreSQL migration creates.
- `values.schema.json` rejects multi-worker Prometheus deployments without a `multiprocDir`.

### Added

- `flask backfill-aggregate` and `flask enable-retention` commands for the TimescaleDB lifecycle (#43).
- Two TimescaleDB continuous aggregates (`request_counts_hourly_by_entity`, `request_metrics_1m`) so charts stop scanning raw rows.
- Compression for `request_logs` chunks older than 7 days.
- `lumen_stream_aborts_total{source,reason}` metric for mid-stream disconnects.
- `request_logs.aborted` replaces the `cost = 0` abort convention.
- Seven new `request_logs` timing columns (`started_at`, `queue_wait`, `preflight`, `ttft`, `ttft_visible`, `send_blocked`, `outcome`).
- Live per-model request state, shared fleet-wide when Redis is configured.
- `rate_limiting.storage_url` is now flagged restart-required.
- `/metrics` is now a pure memory read backed by a background snapshot thread (+ `lumen_metrics_snapshot_age_seconds`).
- A `LiveState` seam with per-ticket deadlines and per-model in-flight/user counts.
- New queue metrics (`lumen_wsgi_queue_depth`, `lumen_wsgi_threads_{busy,total}`, `lumen_wsgi_queue_wait_seconds`, `lumen_rejections_total`).
- `lumen_db_pool_wait_seconds` records time spent acquiring a DB connection.
- Coin exhaustion returns `insufficient_quota` + `Retry-After`; rate limits now send `Retry-After` (#40).
- Health probing is elected to one process per pod via `flock`.
- ASGI bridge tracks queue wait and running depth, and skips clients that hung up while queued.
- `/metrics` is now correct across multiple WSGI processes (dead-worker reaper, PID reuse, bounded labels).
- Multi-process `/metrics` scale-out: `wsgiProcesses` Helm value, optional `serviceMonitor`, shared `multiprocDir` (#43).
- Unknown keys under `app:` now log a warning on startup/hot-reload.
- `pytest-timeout` added as a CI hang backstop.
- SQLAlchemy engine/pool DEBUG lines controlled by `app.database.logging`.

## [1.25.0] - 2026-08-14

### Changed

- Per-user `/usage` queries read the `request_counts_hourly_by_entity` aggregate (with a raw fallback) so a retention policy is safe to enable.
- CI TimescaleDB pinned to `2.27.2-pg17` (the production version).
- SkipTo button hidden until keyboard focus.
- Profile page reorganised into deep-linkable tabs (Chat & API Keys, Projects, Models).

### Added

- Admin mode: admins act as normal users until they enable an admin-mode switch on their profile.
- Users can delete all webchat conversations and disable conversation storage entirely; conversation count is now a lifetime counter.

## [1.24.2] - 2026-08-11

### Added

- A model `url` may be a bare HuggingFace repo id (auto-expanded to `https://huggingface.co/<id>` on sync).

## [1.24.1] - 2026-08-11

### Fixed

- Disabled models show their HuggingFace README on the detail page (the lookup no longer filters on `active`).

## [1.24.0] - 2026-08-08

### Added

- Connect page and guide now include an **R** example via the `ellmer` package.

### Security

- Fixed stored XSS in the admin users table (unescaped display names).
- Fixed stored XSS on the project detail page (`onclick` string breakout).
- Closed a billing/quota bypass in the OpenAI proxy: forwarded client fields are now a strict allowlist.
- Prefix-cache isolation between users via a derived `cache_salt`.

### Fixed

- `flask db upgrade/downgrade` error out clearly on SQLite instead of failing mid-migration.
- Fixed `marked is not defined` in the graylist consent modal.
- Coin refill no longer clobbers concurrent deductions (single atomic UPDATE).
- Users on the global default coin pool are now refilled.
- First-login balance initialisation stamps naive UTC; the refiller tolerates aware timestamps.
- `subtract_coins()` no longer attempts an INSERT on every request.
- Fixed a permanent DB connection-pool leak in streaming endpoints — `stream_with_context` is banned; streaming generators now run context-free.

### Changed

- The number of concurrent requests per worker is configurable via `LUMEN_WSGI_WORKERS` (Helm `wsgiWorkers`).
- Streaming `/v1/chat/completions` now records `duration` in `request_logs`.
- Minimised the Docker image (multi-stage build, smaller runtime).

## [1.23.0] - 2026-07-15

### Added

- Projects can have an **owner** who can add/remove managers, transfer ownership, and activate/deactivate the project.

## [1.22.0] - 2026-07-05

### Fixed

- Fixed `KeyError: 'project_ids'` for sessions predating the 1.21.0 client→project rename (nav cache now rebuilt).
- `/metrics` collector explicitly releases its session in a `finally`.
- `/chat/stream` releases its DB session before the streaming loop.
- Health checker no longer stalls on one silently-dropping endpoint (bounded probe thread pool + 10s timeout).

### Added

- Profile shows a **Projects** section (access list + usage stats); appears on the admin user view too.
- Model sync now syncs **pricing** from models.dev; queries SGLang `/get_server_info` (authoritative limits/modalities); manager names link to profiles.

## [1.21.0] - 2026-07-05

### Fixed

- Fixed a DB connection-pool leak in the chat streaming endpoint (conversation id captured pre-commit, session released before final yield).

### Changed

- Connect page config.json includes each model's `limit` and `cost`.
- **BREAKING:** "client" renamed to "project" across the codebase (config, DB migration `c4d5e6f7a8b9`, URLs, API payload).

## [1.20.0] - 2026-07-03

### Added

- Clients can be added to **groups** for model-access resolution.

### Fixed

- Admin coin-pool edits now apply immediately (per-user limits re-synced on reload); changing starting coins resets the balance.
- Config editor no longer returns secrets in plaintext (masked + restored on save).
- UI-created clients are now recorded in `config.yaml`.
- Consecutive leading system messages are merged for providers that reject multiples.

### Changed

- Clients list is server-side paginated with search and a "Show disabled" toggle.

## [1.19.0] - 2026-06-29

### Added

- **Connect your tools** page (`/connect`): generated OpenCode config, curl, and Python examples.
- Thanks to Josh Henry for the idea.

### Fixed

- "Total Users (Cumulative)" graph now shows the actual total user count (seeds the running total with pre-window users).

### Changed

- Models dashboard: "Total Endpoints" column replaced with an "Acknowledgment" pill.
- Explicit group memberships refresh on config reload (rule-based ones still update at login).

## [1.18.1] - 2026-06-26

### Fixed

- The "Require acknowledgement consent for API requests" toggle can now be turned off (writes `consent: false`).

## [1.18.0] - 2026-06-26

### Fixed

- Model detail page renders for blocked/disabled models instead of 404.
- Themed 404/500 pages (API routes still return JSON).
- New clients immediately get their configured coin pool and model-access defaults.
- `input_modalities` migration no longer fails on SQLite.
- Health checker no longer holds a DB transaction open across network probes.

### Changed

- Audio pricing is **per hour** (`audio_cost_per_hour`); legacy per-minute key still accepted with a deprecation warning.
- **Simplified model access/tokens in `config.yaml` new `version: 2` format** (`access`, `needs_ack`, `disabled`, `ack_message`), a `defaults` block, and a `config_editor` flag; graylist is migrated to per-model `needs_ack`.

## [1.17.1] - 2026-06-19

### Fixed

- Long-lived pages refresh the CSRF token (30s timer + on focus) so write actions no longer 400 after an hour.

## [1.17.0] - 2026-06-20

### Added

- Reject OAuth logins with unverified email (new `oauth2.allow_unverified_email` flag).
- `lumen_db_pool_connections` gauge + 80%-capacity warning.
- `/v1/audio/transcriptions` and `/v1/audio/translations` (speech-to-text, billed per minute/`audio_cost_per_minute`).

### Fixed

- API endpoints pass through upstream 4xx with real status; 5xx still generic.
- Model sync no longer sets `max_output_tokens` to `max_model_len`.
- Usage pages no longer 500 on null token totals.
- Client disconnects record a zero-cost `request_logs` entry.
- Invalid JSON/Content-Type on `/v1` returns a JSON error.
- Dev login gated on debug mode (not `remote_addr`).
- Streaming API sends terminating `data: [DONE]` after mid-stream errors.
- Config editor backs up `config.yaml.bak` before saving.
- `subtract_coins` uses a single atomic UPDATE floored at 0.

### Changed

- Shared `best_group_pool_limit` helper; `get_model_access_status` delegates to `bulk_model_access_info`; shared `_complete_and_bill`; one lookup pass per request.
- All models migrated to SQLAlchemy 2.x `Mapped`/`mapped_column`.
- Updated all locked dependencies.
- Centralised UTC handling in `lumen.timeutils.utcnow()`.
- Pure performance cleanups: preload models on sync, single resolve in billing.
- `/metrics` totals exported as counters; cost total renamed to `lumen_model_cost_coins_total` (**breaking**).

## [1.16.3] - 2026-06-16

### Added

- Automatic PostgreSQL connection-pool sizing from `max_connections` (60/20/20 across workers × replicas); pre-ping always on.

### Fixed

- Helm chart synced with the current config schema (`app.database`, `api.*` values).

## [1.16.2] - 2026-06-14

### Changed

- Documentation: consolidated load-testing guide, added `/usage` guide.

### Fixed

- Documentation corrections (API key prefix, config keys, endpoints).
- Load-test script reads the monitor token from `api.monitoring.token`.

## [1.16.1] - 2026-06-14

### Fixed

- Monitor-token API auth and `/metrics` read config from `api.monitoring`/`api.prometheus`.

## [1.16.0] - 2026-06-13

### Added

- New **Usage** page (`/usage`) and a bar-chart per user; renamed "Analytics" nav to "Usage".

### Changed

- Config editor: prometheus/monitoring as sub-cards; `prometheus`/`monitoring` keys moved to the top level of config.

### Fixed

- Group membership rules now require **all** conditions (AND) instead of any one (OR).
- `/metrics` and monitor auth read config from the top-level keys.
- Web chat streaming releases its DB connection before the LLM call.

## [1.15.2] - 2026-06-13

### Fixed

- Config editor writes to a `/tmp` temp file; shows a read-only banner when the config is not writable.

## [1.15.1] - 2026-06-13

### Fixed

- Test imports/expectations updated for the `app.database` config block.

## [1.15.0] - 2026-06-13

### ⚠ Migration Required

- `app.database_url` and `app.db_pool` replaced by a single `app.database` block (app will not start without it).

### Added

- Admin config editor at `/admin/config` with live forms and atomic writes; "Update"/"Update All" model sync.

### Fixed

- Connection-pool exhaustion under load (scalars extracted, `db.session.remove()` before the LLM call).
- Announcement dismiss key hashes the full HTML content.

### Changed

- Config editor sections, ordering, banking of unrecognised fields; self-removal guard.

## [1.14.0] - 2026-06-11

### Added

- `api.consent` flag (exempt API requests from graylist consent); `app.graylist_default_notice` fallback; consent indicators on chat/profile.

## [1.13.0] - 2026-06-06

### Removed

- Conversations are always permanently deleted (`chat.remove` soft-delete removed); Helm migration Job removed.

### Added

- `/healthz`; `wait-for-db` init container; non-root `lumen` user; CI pushes to `ghcr.io` too; `flask-limiter[redis]`; many new Helm chart values (models, config, announcement, emailThemes, logs, oauth2 params).

### Fixed

- Migration idempotency; chart migration-job env/order/security fixes; Chart.yaml version.

### Changed

- Default image repo on Docker Hub; default TimescaleDB `2.27.2-pg17`.

## [1.12.0] - 2026-05-21

### Added

- Dismissable announcement banner (`localStorage`-backed); `app.email_themes` mapping.

## [1.11.2] - 2026-05-17

### Fixed

- Naive-UTC `last_refill_at`; `subtract_coins` zeroes short balances and seeds balance rows; N+1 fixes on `/v1/models`, health, sync; rate limit on `chat_upload`; conversation pagination cursor/lookup hardening; PDF error leak; many smaller robustness/security fixes (see git log).

## [1.11.1] - 2026-05-17

### Fixed

- Help sidebar hides developer-only docs; release link no longer double-prefixes `v`.

## [1.11.0] - 2026-05-17

### Fixed

- Migration uses composite `PRIMARY KEY (id, time)` for TimescaleDB.

### Added

- Helm chart at `chart/` (TimescaleDB+Redis, Ingress/Gateway API, migration Job, optional in-cluster inference); RWX model storage PVCs; `storage.prefetch` hook Job.

## [1.10.0] - 2026-05-17

### Added

- Chat message metadata shows the model name; hidden thinking tokens when zero.

### Changed

- General cleanup: timezone-aware datetime comparisons, shared graylist consent modal, HTTPStatus constants, "Usage" page renamed to "Profile" (`/profile`), decomposed hot-path functions, deferred-import fixes.

### Fixed

- Timezone-aware refill arithmetic; single-head migration test; `with`-managed OpenAI clients; atomic `update_stats` savepoints; bulk N+1 resolutions; SSRF/security hardening on uploads and analytics.

### Database

- Migrations for `NOT NULL` balances/keys, head merge, indexes (`model_endpoints`, `entity_managers`, conversations, messages, FKs).

### Accessibility

- Sortable table semantics, modal labels, heading hierarchy, canvas fallback text, `aria-*` fixes, skip navigation for all themes.

### Security

- Sanitised filenames/dates; generic error messages; refusal to start with `DEV_USER` in production; validation of analytics `period`; `hmac.compare_digest`; `send_from_directory`; security headers; cookie flags; startup warnings for in-memory rate limiting and unauthenticated `/metrics`.

## [1.9.3] - 2026-05-11

### Added

- Version/commit in help sidebar; reasoning/thinking saved to DB and re-shown; token popup splits thinking vs output; announcement banner config.

### Fixed

- vLLM models emitting `delta.reasoning` now capture chain-of-thought.

## [1.9.2] - 2026-05-10

### Added

- Broader negative/else-branch test coverage and a spec-compliant OpenAI model mock.

### Fixed

- `Invalid Date` timestamps (`formatTimestamp` double `Z`); deprecated query patterns removed; `EntityBalance` rows stamped with `last_refill_at` so the refiller picks them up.

### Changed

- Per-user `EntityLimit` always wins over group limits in budget resolution.

## [1.9.1] - 2026-05-09

### Fixed

- Removed the SkipTo.js-only accessibility test check.

## [1.9.0] - 2026-05-09

### Added

- Theme system (`theme.yaml`, partials, per-theme static; built-ins `illinois`, `default`, `uic`, `uis`); "About Illinois Computes"/feedback sections; CSRF via Flask-WTF.

### Changed

- `default` theme is the fallback; all queries modernised to `db.session.execute(select(...))`.

### Fixed

- N+1 query fixes on `/models`, `/chat`, `list_conversations`; `Z`-suffixed datetimes in chat JSON.

## [1.8.0] - 2026-05-09

### Added

- `entity_stats` pre-aggregated table; admin help docs; `LUMEN_SECRET_KEY`; dev-server docs watch; per-user admin usage view; `dbschema.md`; schema comments.

### Changed

- Date columns sort descending; admin users table redesign; model docstrings/comments.

### Removed

- Admin Groups page and admin per-user limits page; top-level `model_access:` config.

## [1.7.2] - 2026-05-04

### Changed

- Help docs navigation restructured around `docs/nav.json`; simplified `/help` slugs; relative docs/image links rewritten.

## [1.7.1] - 2026-05-04

### Added

- Help accessible without login.

### Changed

- Doc image paths made relative.

## [1.7.0] - 2026-05-04

### Added

- Help documentation at `/help`; autocomplete Add Manager dialog; clients (service accounts) with managers, detail pages, config, and tests; `app.dev_user` dict support.

### Changed

- Usage page gains dedicated API Keys and Model Access sections; new clients don't auto-add the creator as manager; `access_type` replaces `allowed`.

## [1.6.1] - 2026-05-02

### Added

- Expanded test suite (218 tests), WCAG accessibility test suite, GitHub Actions CI, `.gitignore` coverage entries, `dorny/test-reporter`.

### Fixed

- Removed `datetime.utcnow()` and `Model.query` legacy usage; label/heading/overflow WCAG fixes.

### Changed

- Model-detail request-count and `/v1/models` rate queries ported to SQLAlchemy for SQLite/dialect compatibility.

## [1.6.0] - 2026-05-02

### Added

- Model detail page, `notice` field, model-name links, endpoint up/down badges, whitelist/blacklist/graylist model access, consent flow.

### Changed

- Removed the admin dashboard chevron rows; HuggingFace README styling; `models:` group key deprecated for `model_access.whitelist`.

## [1.5.1] - 2026-05-01

### Fixed

- Chat streaming crash for non-reasoning models.

### Changed

- Model picker hides models with no healthy endpoints.

## [1.5.0] - 2026-05-01

### Added

- Streaming reasoning "Thinking…" blocks; coin-based budget; reset-token button; model config fields; file attachments (text + image) with server-side validation.

### Changed

- Token budget → **coin** system; `input_modalities` replaces `supports_vision`; 2-decimal price displays.

## [1.4.0] - 2026-03-28

### Added

- Token-by-token streaming; model `description`/`url`; token balance init at login; configurable `app.logs.level`.

### Fixed

- Analytics heatmap shows local timezone.

## [1.3.0] - 2026-03-26

### Added

- Footer links; Prometheus `/metrics`; TimescaleDB `request_logs` + continuous aggregate; Admin Analytics page; `dev.sh` TimescaleDB container.

### Changed

- Unified token pools; model access independent of pool; group config format; default DB to TimescaleDB.

### Fixed

- Admin users page reads stats from `model_stats` (chat + API).

## [1.2.1] - 2026-03-23

### Fixed

- Chat timestamps showed `Invalid Date` with PostgreSQL (`strftime` used instead of `isoformat`).

## [1.2.0] - 2026-03-23

### Added

- LaTeX math rendering (KaTeX); `app.dev_user` bypass; separate admin Users/Groups pages with server-side pagination; model health dashboard; if applicable `app.logs.model`; self-hosted KaTeX/SkipTo.js; full WCAG 2.1 AA compliance.

### Fixed

- Forward `/v1` tool params to upstream; idempotent PK sequences; ON DELETE CASCADE FKs.

## [1.1.0] - 2026-03-21

### Fixed

- `openai.OpenAI` used as a context manager in all call sites (fixes EMFILE); admin nav/links shown via context processor.

### Changed

- All timestamps shown in the user's local timezone.

### Added

- Locust load-testing toolkit; configurable DB pool settings.

### Security

- Per-endpoint rate limiting; default secret keys refused at startup; API keys stored as HMAC hashes; disabled users blocked; admins re-verified per request; refills via background task only.

### Changed

- Package renamed `illm` → `lumen`.

## [1.0.0] - 2026-03-20

### Added

- Initial production release: OpenAI-compatible web chat, CILogon login, token budgets, groups, admin panel, load balancing, conversation history, markdown, API endpoint, model health dashboard, hot-reload, Illinois branding, Docker.
