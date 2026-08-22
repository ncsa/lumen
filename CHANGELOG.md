# Changelog

All notable changes to Lumen will be documented in this file.

## [Unreleased]

### Added

- **Group auto-join rules moved into the database.** Each group now carries its own login auto-assignment rules (new `group_rules` table + `groups.auto_join` flag, migration `ac3d4e5f6a7b`), edited on an admin-only **Rules** tab of the group detail page — field, contains/equals matcher, and value; a user matching **all** rules is added at sign-in and removed when they stop matching; deactivating a group pauses its auto-join (memberships are removed at each member's next login and return after reactivation). Admins can also enable auto-join with rules directly in the New Group dialog; auto-join cannot be saved without at least one rule (an empty rule set fails closed). An auto-join group is fully automatic: it cannot have an owner, and members cannot be added or removed by hand — turning auto-join off converts it back to manual management and freezes the current roster (auto-assigned members become manual instead of draining at each login). Rule matching is type-safe: rules on boolean, numeric, or list-valued claims (like CILogon's `is_member_of`) match against each element's string form instead of crashing the login callback. Projects gained the same one-owner-per-project unique index as groups (migration `ag7b8c9d0e1f`, healing any existing double-owner rows), and ownership transfers on both pages order the demote before the promote so transfers to older members no longer trip the index. The `config.yaml` `group_rules:` section is **deprecated**: migration `af6a7b8c9d0e` imports it into the database exactly once (creating missing groups with their rules, attaching rules to rule-less existing groups, and clearing auto-join on groups left without rules) and it is ignored afterwards with a startup warning — the database is the source of truth, so rules deleted in the UI stay deleted, and the config editor's Group Rules section is gone. `groups.config_managed` was dropped; group names are freely editable again. `app.dev_user.groups` naming a nonexistent group is ignored instead of requiring the group to exist.
- **Group management UI.** A new **Groups** page (`/groups`, linked from the nav for every signed-in user) lists the groups you belong to — members, granted models, member usage, and the group's coin policy — with the same sorting, paging, and search as the Projects page. Any user can create a group and becomes its owner; admins can additionally name an owner and set Max Coins / Refill Rate at creation. The group detail page mirrors the project detail page: a profile card with the group's stats, an editable profile (name, description, active — plus coin policy for admins), a sortable and paginated **Members** tab that records when each member joined and where the owner adds users *or* projects, removes members, and transfers ownership via a **Change Owner** dialog (the new owner must already be a member), and a **Models** tab where the owner grants models they own to the whole group and revokes them again. "Add Model" is disabled when you own no models left to grant. A group auto-created from a `config.yaml` group rule is fully manageable except its name, which the rule matches on. A group with **no owner** hides its member list and rolled-up usage from everyone but administrators (and is excluded from the summary totals), so a large auto-assigned group from `group_rules` cannot be used to browse the user directory; the restriction is enforced by the API, not just the UI. Lowering a group's Max Coins clamps affected member balances to their best remaining pool. Adds a `group_members.is_owner` column (migration `aa1b2c3d4e5f`) and a one-owner-per-group unique index (migration `ab2c3d4e5f6a`).
- Models can be marked **early access** (`early_access: true` in `config.yaml`): users must acknowledge that the model may change or be removed before using it. This works alongside the existing `needs_ack` acknowledgment — a single dialog acknowledges both at once — and the consent record now tracks each requirement separately, so a model that gains a new requirement after a user consented prompts them once more. The early-access warning text is configurable via `defaults.models.early_access_message`. Early-access models show an "early access" badge on the models list and detail pages.
- Models can have an **end date** (`end_date` in `config.yaml`, date or UTC datetime): after it passes, the model is hidden from the chat picker, models pages, and API model list, and any use is rejected — the same treatment as `disabled`. Without an end date the model remains usable indefinitely. A future end date is shown as "Available until" on the model detail page.
- The model detail page shows **First seen** — when the model was first added to the system.

### Changed

- The implicit **`default` group is gone**. The login sync no longer auto-joins every user to a group named `default`, the profile page no longer hides it, and migration `ae5f6a7b8c9d` deletes the group when it is machine-shaped — no model grants and no usable pool — cascading its auto-created memberships; a `default` group carrying real policy is kept as an ordinary group for the operator to dispose of. Users without any group pool fall back to `defaults.tokens` in config.yaml, exactly as before; a group named "default" is now an ordinary group.
- Project ownership is transferred through a **Change Owner** dialog next to “+ Add Manager” (replacing the per-row “Make Owner” buttons), and the new owner must already be a manager of the project — transferring to an outsider no longer silently adds them.
- CLI commands no longer start the background workers. Loading the app for `flask db upgrade` (or any other command that is not `flask run`) also ran the health checker, coin refiller and config watcher, which queried the database with the current models while the schema was still on the previous revision — so any migration adding a column raced a burst of `column ... does not exist` tracebacks. create_app now detects a non-serving CLI invocation via Flask's FLASK_RUN_FROM_CLI marker plus the click command name (argv cannot be used: the app module is itself named `run`; and a bare click-context check would misfire under uvicorn, whose console script is itself a click command). Serving processes and `flask run` are unaffected, and `BACKGROUND_WORKER=false` still works as before.
- Linting is now enforced by a dedicated **Lint** workflow with one job per language: **ruff** for Python (configured in `[tool.ruff]`), **yamllint** for YAML (configured in `.yamllint.yml`), and **helm lint** for the chart. Ruff runs pycodestyle errors, pyflakes, import sorting, and pycodestyle warnings; `E501` is not enforced, and the `DTZ`/`UP017` rules are deliberately off because they conflict with the project's naive-UTC convention. Yamllint uses a relaxed profile and skips Helm templates (Go templates are not valid YAML until rendered — `helm lint` covers those) and anything in `.gitignore`. Existing violations were fixed — import blocks sorted, ~20 unused imports removed, and module-level imports in `llm.py`, `chat/routes.py`, `api/routes.py`, and `config_watcher.py` hoisted above the code that had been separating them.
- **Breaking (Lumen 2.0): config version 3 is required.** The app refuses to start unless `config.yaml` declares `version: 3`, and the config watcher skips hot reloads of older files. The `users:`, `projects:`, `clients:`, and `groups:` sections were removed: groups, memberships, and coin pools are managed in the database (existing rows keep working; group and model management dialogs are planned), and OAuth auto-assignment rules moved to a slim top-level `group_rules:` section (`<group-name>: [{field, contains|equals}, ...]` — a group named there is created if missing; `app.dev_user.groups` still works for dev login). The admin config editor's Groups and Users sections were replaced by a Group Rules section. The Helm chart emits version 3 and its `groups`/`users` values were replaced by `groupRules`; old values files fail schema validation and must be migrated.
- **Breaking:** model access control is now **ownership-based** and replaces the allow/block lists entirely. A model can have an owner (a user, set by an admin): a model without an owner is available to everyone; an owned model is available only to its owner and to members of groups it has been explicitly granted to. Owner and group grants live in the database and are edited via a new admin-only **Access** card and Edit Access dialog on the model detail page — they are not part of `config.yaml`. The old mechanisms were removed: the per-model `access:` key, group/user `model_access:` blocks (and legacy `whitelist`/`blacklist`/`graylist` lists), `defaults.models.access`, and the `entity_model_access`/`group_model_access` tables plus the per-entity/per-group access defaults. **Take a database backup before upgrading** (downgrading the migration recreates the old access tables empty — the old rules are not restored), and note that **after the migration every non-disabled model is available to everyone until an admin assigns owners** — set owners on restricted models immediately after deploying. The acknowledgment flow (`needs_ack`, `ack_message`, `early_access`) and the lifecycle flags (`disabled`, `end_date`) are unchanged and stay in `config.yaml`.
- **Breaking:** projects and per-user coin pools are no longer configured in `config.yaml`. The `projects:` section (limits, model access, groups) and the `max`/`refresh`/`starting`/`pool` keys under `users:` are ignored (with a startup warning); project creation no longer writes entries into the file, and the admin config editor's Projects section and per-user Token Pool card were removed. Values previously synced into the database remain in effect. Instead, an **Edit** button above the stats on the project detail page lets the owner or an admin change the project's name and active flag (admins can also set Max Coins and Refill Rate), and the same Edit button on a user's profile (admin only, in admin mode or via the admin Users page) enables/disables the user and sets their coin pool. Lowering Max Coins clamps the entity's current balance.

- Project detail page redesigned to match the user profile layout: profile header card with project identicon and stats, and Managers / API Keys / Models tabs.
- Native browser dialogs (`alert()`, `confirm()`, `prompt()`) replaced everywhere with a styled, accessible Bootstrap modal via new shared `appAlert`/`appConfirm`/`appPrompt` helpers in app.js (enforced by `tests/unit/test_no_native_dialogs.py`).
- Navigation bar: Projects moved between Profile and Models (all themes).
- The projects table now matches the admin users table: Created, Last Used, Coins Left, and Coins Spent columns, plus admin buttons for view-usage and reset-coins alongside the activate/deactivate toggle. Both the projects and admin users tables now sort by Last Used (newest first) by default.
- The projects table's details (sliders) button was replaced by an edit (pencil) button that opens the project edit dialog inline (enabled for the project owner and admins; disabled with an explanatory tooltip otherwise), and the admin users table gained the same pencil for editing a user's active flag and coin pool.
- The projects table and the Projects menu entry always include deactivated projects (admins see all projects, managers see the ones they manage) so owners can reach and re-enable them; the "Show disabled" toggle was removed.
- Refill Rate on the profile and project pages now always shows when the next refill happens — the actual local clock time next to the countdown, or "after first use" when no refill is scheduled yet — and all header stat boxes keep a uniform height.

### Fixed

- The test suite was silently running against the developer database: `tests/fixtures/test_config.yaml` used a `database_url:` key the app never reads, so the test app fell back to the default `sqlite:///lumen_dev.db` and the suite's teardown dropped every table in it on each run. The fixture now uses the real `app.database.url` key (`test_lumen.db`), and the cleanup path points at the instance directory where Flask-SQLAlchemy actually puts relative SQLite paths.
- All data tables now consistently draw cell borders: the API keys, projects, and models tables on the profile page, the managers/keys/models tables on the project detail page, the projects list, and the admin users table gained the borders that the models dashboard and Web Chat table already had.
- Model sync endpoint probes are hardened against SSRF: endpoint URLs must be http/https, hostnames that resolve to private, loopback, link-local, multicast, or reserved IP ranges are refused, and the probes no longer follow redirects — so the admin "Update from endpoint" feature cannot be used to reach internal services.
- Request bodies are now capped via `MAX_CONTENT_LENGTH` (2× the chat upload `max_size_mb`, with a 100 MB ceiling), so oversized uploads are rejected with 413 before being buffered into memory.
- Knowledge cutoff values synced from models.dev are normalized to `YYYY-MM`: models.dev sometimes reports full `YYYY-MM-DD` dates, which overflowed the 7-character database column on PostgreSQL.
- Saving from the admin config editor failed with "Permission denied: ./config.yaml.bak" in container deployments where only config.yaml itself is bind-mounted: the pre-save backup is now best-effort (logged as a warning) instead of aborting the save.
- The admin "Reset coins" button refilled to a stale amount after Max Coins was changed in the Edit dialog: editing Max Coins now also updates the starting balance the reset refills to.
- Blanking Max Coins in an Edit dialog now removes the entity's own coin pool so it falls back to the inherited group/default pool (previously blank fields were silently ignored).
- "Make Owner" and "Remove" buttons on the project detail page did nothing: the manager's name was JSON-encoded inside a double-quoted `onclick` attribute, truncating the handler at the name's opening quote (`Uncaught SyntaxError: Unexpected end of input`).
- CI test failures (`ModuleNotFoundError: No module named 'httpx'`): the openai 3.x SDK now uses `httpx2`, and the upstream-error tests were updated to match. Dependency floors were raised to the majors actually locked and tested (`openai>=3`, `flask-limiter>=4`, `pypdf>=6`, `psutil>=7`, `pytest>=9`, `pytest-cov>=7`).

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
