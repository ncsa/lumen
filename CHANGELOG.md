# Changelog

All notable changes to Lumen will be documented in this file.

## [Unreleased]

### Added
- Clients can be added to **groups** via a `groups:` list in their `config.yaml` entry (and via checkboxes in the Config editor's Clients tab). Group membership grants the client access to models its own rules would otherwise block, using the same group model-access resolution as users. Memberships are config-managed — added on reload and removed when dropped from the list.

### Fixed
- Admin config edits to a user's coin pool (max coins, refresh rate, starting coins) now take effect on the user's profile immediately on save, instead of waiting for the user to log out and back in. Per-user `EntityLimit` rows are now re-synced from `config.yaml` on every config reload (and at startup), closing an asymmetry where clients and groups were re-synced on reload but users were only synced at login. Changing an existing per-user **starting coins** value also resets the user's live "Coins Available" balance to the new value; changing only max/refresh leaves the accrued balance untouched, and adding a first per-user block for a user on the global pool preserves their accrued balance.
- Clients created through the `/clients` UI are now recorded in `config.yaml` with an empty entry (`<name>: {}`) so the file always reflects which clients exist. Previously a UI-created client lived only in the database and was invisible to the Config editor, so the next config save could silently drop it. An empty entry falls back to `clients.default`, so newly created clients keep the default budget and access. On startup, any pre-existing clients that were only in the database are backfilled into `config.yaml` the same way (when the file is writable and the Config editor is enabled).
- Chat completions now merge consecutive leading system messages into a single system message before calling the upstream model, for providers that reject or ignore more than one system message. Applies to both streaming and non-streaming requests.

### Changed
- The Clients list is now server-side paginated (matching the Users list) with a per-page selector and search. Disabled clients are hidden by default; admins get a **Show disabled** toggle to reveal them.

## [1.19.0] - 2026-06-29

### Added
- A **Connect your tools** page (`/connect`) that generates ready-to-use client configuration: a downloadable OpenCode (`opencode.ai`) `opencode.json` pre-filled with every model your account can access (keyed off the `LUMEN_API_KEY` environment variable), plus copy-paste **curl** and **Python** examples. Picking a specific model tailors the curl/Python snippets, including vision (`image_url`) examples for image-capable models and chat + `/v1/audio/transcriptions` examples for audio-capable models. The page is linked from the Models dashboard, each model detail page (pre-selecting that model), and the API-key sections of the profile and client pages, and is documented in a new "Connect Your Tools" help guide. Thanks to Josh Henry for the idea.

### Fixed
- The "Total Users (Cumulative)" usage graph now shows the actual total user count instead of only the users created within the selected period. For non-"all" periods (week/month/year) the cumulative line restarted from zero at the start of the window, so an installation with 105 users would show 1–5 over the week. The query now seeds the running total with the count of users that existed before the window.

### Changed
- The Models dashboard (`/models`) replaced the redundant "Total Endpoints" column (its value already appears as the denominator in the "Healthy" column) with an "Acknowledgment" column that shows a yellow "required" pill when a model requires user acknowledgement before use.
- Explicitly-assigned group memberships (`users.default.groups` and `users.<email>.groups`) now refresh on config reload (and at startup) instead of waiting for the user to log in again. Editing a user's groups in `config.yaml` takes effect immediately. Rule-based ("auto") group memberships still update at login, since they depend on CILogon attributes only available then.

## [1.18.1] - 2026-06-26

### Fixed
- The admin config editor's "Require acknowledgement consent for API requests (/v1/*)" toggle can now be turned off. Unchecking it previously dropped the `api.consent` key from the saved config, which the reader defaults back to `true`, so consent stayed required no matter what. The editor now writes `consent: false` explicitly.

## [1.18.0] - 2026-06-26

### Fixed
- The model detail page (`/models/<name>`) no longer returns a 404 for blocked or disabled models. Blocked models now render the page with the existing "Access denied" notice (the template already had the branch, but the route aborted before reaching it); disabled models render instead of 404ing. Links to these models (e.g. from the profile "Models & Access" table) now resolve.
- Added themed `404` and `500` error pages so a mistyped or stale URL shows a branded "page not found" with a link home instead of the bare default error page. API routes (`/v1/*`) still receive a JSON error body.
- Clients created through the Clients page (or `POST /clients`) now immediately receive their configured coin pool and model-access defaults (`clients.default` / a named `clients.<name>` entry). Previously a new client was created with no pool, so it fell back to the global token defaults and saw every model as blocked until the next config reload.
- The `input_modalities` schema migration no longer fails on SQLite (it used PostgreSQL-only `::json`/`::text` casts), so a fresh local dev database can run `flask db upgrade` again. PostgreSQL behavior is unchanged.
- The background endpoint health checker no longer holds a DB transaction open across its per-endpoint network probes, which left the Postgres connection `idle in transaction` and could trip `idle_in_transaction_session_timeout`.

### Changed
- Audio (speech-to-text) pricing is now expressed **per hour** (`audio_cost_per_hour`) instead of per minute — cheap ASR rates like `$0.10/hour` no longer need many leading zeros. The DB column is renamed and existing per-minute values are migrated ×60; the legacy `audio_cost_per_minute` config/Helm key is still accepted (converted, with a deprecation warning) and the config editor migrates it on load/save.
- **Simplified model access, token limits, and model status in `config.yaml` (new `version: 2` format).** Model access is now expressed with three orthogonal per-model fields instead of the old per-scope `whitelist`/`blacklist`/`graylist` lists:
  - `access: allowed | blocked` — the model's own default (optional; leave unset to inherit scope defaults). When set it ranks **above** group/user *defaults* but below an explicit per-scope `allowed`/`blocked` rule — so a model can be blocked-by-default yet enabled for a specific group or user (no `default` group needed).
  - `needs_ack: true | false` — requires user acknowledgement before use. This is a sticky model-level property: no group/user/client override can remove it.
  - `disabled: true | false` — hard off; the model is hidden everywhere and **cannot** be overridden by any scope (replaces `active: false`). Permanently removing a model means deleting it from config.
  - `ack_message` — optional per-model acknowledgement message, overriding the global default.
  Groups/users/clients now only set the allow/block axis (`allowed:`/`blocked:` lists + `default:`). The legacy `whitelist`/`blacklist`/`graylist` keys and `active:` are still accepted as input with a deprecation warning (`graylist` maps to `allowed`; acknowledgement is now set on the model).
- New top-level `defaults` block: `defaults.models.access` / `defaults.models.ack_message` (the global ack message, moved off `app.graylist_default_notice`) and `defaults.tokens.{max,refresh,starting}` — a global token (coin) pool that groups/users/clients override only where they differ, and a final fallback so an entity with no limit isn't blocked by default.
- New `app.config_editor` flag (default `true`; the Helm chart sets it `false`). When `false`, the `/admin/config` editor is read-only — for git-managed configs.
- The admin config editor gained **Defaults** and **Users** sections (per-user groups, token pool, and model access), labels OAuth-mapped (rule-based) groups, and saves in the `version: 2` format. The "add user" and client "add manager" dialogs now share one typeahead component (`static/js/user-search.js` + a Jinja macro) and auto-focus the search box; a new `GET /admin/api/users/search` endpoint backs the config-editor user search.
- The config editor's per-scope **model access** is now set with a **search-driven widget** instead of free-form Allowed/Blocked textareas: search the enabled models, set each to Inherit / Allow / Block, and see the **effective access and where it's decided** (set here, model default, a group, or the global default). Scales to large model counts (only overrides + searched models render). Users now support the full `model_access` (`allowed`/`blocked`/`default`) like groups/clients; the legacy allowed-only `users.<email>.models:` list still parses.
- DB: `model_configs.active` replaced by `access`/`needs_ack`/`ack_message`/`disabled` columns (`active` remains as a derived read-only property); `group_model_access`/`entity_model_access`/scope defaults migrated from `whitelist`/`blacklist`/`graylist` to `allowed`/`blocked`. The internal access status `graylist` is renamed `needs_ack`.
- **Acknowledgement (formerly graylist) is preserved on upgrade.** Acknowledgement is now a per-model property (`needs_ack`) rather than a per-scope `graylist` rule. Any model that was graylisted by a specific scope rule is **automatically migrated to `needs_ack: true`** — both by the DB migration (from existing `graylist` access rows) and by the config loader (from a legacy `graylist:` list in a v1 `config.yaml`). The one case that can't be reconstructed is a scope **default** of `graylist` (e.g. a group `model_access.default: graylist`), which named no models; **after upgrading, review groups/clients that used `default: graylist` and set `needs_ack: true` on the models that should still require consent.**

## [1.17.1] - 2026-06-19

### Fixed
- Write actions on long-lived pages (e.g. deleting a conversation after chatting for over an hour) no longer fail with `400 Bad Request`. The CSRF token baked into the page at load expires after `WTF_CSRF_TIME_LIMIT` (1h); the client now refreshes it from a new `GET /csrf-token` endpoint on a 30-minute timer and whenever the tab regains focus. The admin config editor, which kept its own copy of the page-load token, now uses the shared refreshed token too.

## [1.17.0] - 2026-06-20

### Added
- Reject OAuth logins where the provider marks the email unverified (`email_verified: false`); a missing claim is still accepted. New `oauth2.allow_unverified_email` flag (default `false`, hot-reloaded, in the Helm chart and config editor) overrides this.
- `lumen_db_pool_connections` Prometheus gauge (labels `state=size|checked_in|checked_out|overflow|limit`) exported from the `/metrics` endpoint, plus a warning log when the connection pool exceeds 80% of capacity. Surfaces slow connection leaks: a `checked_out` value that climbs and never falls back points to a code path that checks out a pool connection and never returns it.
- `/v1/audio/transcriptions` and `/v1/audio/translations` endpoints (speech-to-text). Billed per minute of audio via `audio_cost_per_minute` on the model config when the upstream reports `usage.type=duration`; falls back to per-token billing otherwise. Adds `audio_seconds` tracking to request logs, model/entity stats, and API keys. Helm chart template, `values.schema.json`, and `config.yaml.example` updated to support `audioCostPerMinute` per model.

### Fixed
- API endpoints now pass an upstream **4xx** (e.g. context-length exceeded) through with its real status and message instead of masking it as a generic `500 "Upstream error. Please try again."`; 5xx/transport failures still return the generic 500. Streaming error chunks now use the OpenAI `{"error": {"message", "type"}}` shape, and upstream-error logs name the failing endpoint.
- Model sync no longer sets `max_output_tokens` to the endpoint's `max_model_len`. `context_window` comes from the endpoint (fallback models.dev `limit.context`); `max_output_tokens` comes only from models.dev `limit.output`.
- Profile/clients/admin usage pages no longer raise a 500 (`TypeError`) when a model has `ModelStat` rows with null input/output token totals; the per-model token total now coalesces null sums to 0.
- When a client disconnects mid-stream (both the chat and `/v1/chat/completions` streaming paths), Lumen now records a zero-cost `request_logs` entry instead of silently dropping the request. Since every hosted model has a coin cost, these aborted requests are findable by `cost = 0`, making disconnect frequency monitorable.
- `/v1/chat/completions` and `/v1/completions` now return the JSON `invalid_request_error` for a missing/wrong `Content-Type` or malformed JSON body, instead of a Werkzeug HTML 415/400 page (`request.get_json(silent=True)`).
- Dev login (`/devlogin`, OAuth bypass) is now gated on debug mode instead of `request.remote_addr`, which a co-located reverse proxy could mask as localhost. It returns 404 when `app.debug` is false even if `dev_user` is set, and a loud warning is logged at startup whenever `dev_user` is configured.
- Streaming `/v1/chat/completions` now sends a terminating `data: [DONE]` after a mid-stream upstream error, so SSE clients don't hang waiting for it.
- The admin config editor now backs up `config.yaml` to `config.yaml.bak` before overwriting, so a partial or malformed save can be recovered.
- Register `EntityStat` in `lumen.models.__init__` so Flask-Migrate autogenerate always sees the table regardless of import order.
- `subtract_coins` now deducts in a single atomic UPDATE floored at 0 (`GREATEST(0, coins_left - cost)`, compiled to `max(...)` on SQLite) instead of a conditional deduct followed by a separate zeroing. This removes a race where a concurrent coin refill/credit landing between the two statements could be clobbered back to 0 (or the request left uncharged).
- The announcement cache-busting `hashlib.md5` call now passes `usedforsecurity=False` so it works on FIPS-restricted builds (the digest is a cache key, not a security hash).

### Changed
- The "best group coin limit" selection (skip 0, unlimited wins, else highest) is now a shared `best_group_pool_limit` helper used by both `get_pool_limit` and the token refiller, instead of duplicated in each.
- `get_model_access_status` now delegates to `bulk_model_access_info` with a single-element list instead of duplicating the full access-resolution pipeline.
- `/v1/completions` and the non-streaming `/v1/chat/completions` now share one `_complete_and_bill` helper (upstream call + billing) instead of duplicating it; each endpoint only shapes its own response.
- Profile/clients/admin pages now resolve model access, endpoints, and the model list once per request instead of twice (the access list and usage list shared the same lookups).
- All models migrated from the legacy `db.Column`/`db.relationship` style to SQLAlchemy 2.x `Mapped[...]`/`mapped_column`/typed `relationship`. Verified schema-identical (no DB migration needed) via DDL diff on both dialects and Alembic `compare_metadata` against a baseline database.
- Updated all locked dependencies to their latest versions (notably SQLAlchemy 2.0.51, openai 2.43.0, cryptography 49.0.0, uvicorn 0.49.0); full test suite passes.
- Minor cleanup: removed a duplicate `datetime` import and an unused `calculate_cost` import, and dropped the unreachable empty-string fallback for `ENCRYPTION_KEY` (the app already refuses to start without it).
- Centralized UTC time handling: added `lumen.timeutils.utcnow()` (naive UTC) and replaced the scattered `datetime.now(timezone.utc).replace(tzinfo=None)` idiom across all models and call sites. No behavior or schema change.
- Config sync now preloads models once per pass instead of issuing a per-model-name lookup for every group/client `model_access` entry (removes an N+1 during `init-db` and config reloads).
- Per-request billing no longer re-resolves model access and the coin pool limit a second time: `check_coin_budget` returns the resolved limit and it is threaded through to `subtract_coins`, halving the access/pool queries on every API and chat request. As a side effect, a graylisted model used via the API when consent is not required is now billed correctly (previously the post-call deduction silently no-op'd).
- The cumulative `/metrics` totals (`lumen_model_requests_total`, `lumen_model_input_tokens_total`, `lumen_model_output_tokens_total`, and the cost total) are now exported as Prometheus counters instead of gauges, matching the `_total` naming convention. Only the `# TYPE` line changes (gauge → counter); existing queries keep working.
- Renamed the `/metrics` cost total from `lumen_model_cost_usd_total` to `lumen_model_cost_coins_total` to match Lumen's coin-based accounting (the value has always been the coin amount). **Breaking for dashboards/alerts** referencing the old name — update them to `lumen_model_cost_coins_total`.

## [1.16.3] - 2026-06-16

### Added
- Automatic database connection-pool sizing on PostgreSQL. The pool is sized from the server's `max_connections`, divided across all worker processes and Kubernetes replicas so combined usage cannot exhaust the server: 60% to `pool_size`, 20% to `max_overflow`, and 20% reserved for psql/migrations/monitoring. Worker count is detected from `WEB_CONCURRENCY` or the uvicorn `--workers` flag; replica count comes from the new `LUMEN_REPLICAS` env var (set by the Helm chart from `replicaCount`). Explicit `app.database.pool_size`/`max_overflow` are still honored when they fit within 80% of `max_connections` across all workers × replicas, otherwise the auto-sized values are used. Added `app.database.max_connections` to override the detected value. Pre-ping is now always enabled (the `pool_pre_ping` config option was removed). SQLite skips pool sizing entirely. If `max_connections` cannot be queried (e.g. the database is briefly unreachable at startup), the app falls back to the configured pool settings instead of failing to boot.

### Fixed
- Helm chart: synced `chart/templates/config-secret.yaml` with the current `config.yaml` schema. Renamed the rendered `app.db_pool` block to `app.database` (and the `config.dbPool` values key to `config.database`) so connection-pool settings take effect again — the app reads pool tuning from `app.database.*` since 1.16.x, so the chart's pool values were being silently ignored. Bumped the chart `maxOverflow` default to 60 to match `config.yaml.example`. Removed the obsolete top-level `model_access:` block (and its `modelAccess` values/schema entries); that section was dropped from the app and is now ignored. Added first-class `api:` values for `api.consent`, `api.prometheus`, and `api.monitoring` (previously only settable via `extraConfig`).

## [1.16.2] - 2026-06-14

### Changed
- Documentation: consolidated the load-testing guide into `loadtesting/README.md` and removed the duplicate, out-of-sync `LOADTESTING.md` (root) and `docs/loadtesting.md`. Added a **Usage** guide page (`docs/guides/usage.md`) for the `/usage` feature and a nav entry for it. Moved the `prometheus` and `monitoring` config documentation under the `api:` section (README, admin config docs, and `config.yaml.example`) to match where the code reads them.

### Fixed
- Documentation: corrected the client API-key prefix in the README (`sk_`, not `lmk-`); removed the nonexistent `max_input_tokens` model field from the model config doc; fixed the "Daily coin budget" wording (it is an hourly-refilled cap, not a daily reset); documented the `api.consent`, `app.theme`, and `app.graylist_default_notice` config keys; clarified that `database_url` accepts SQLite as well as PostgreSQL; and added the `GET /v1/models/<id>` and `POST /v1/completions` endpoints to the API reference.
- `loadtesting/run_loadtest.sh` now reads the monitor token from `api.monitoring.token` instead of the removed top-level `monitoring.token`, matching the 1.16.1 config change; previously its Lumen health-check probe sent no auth header when a monitor token was configured.

## [1.16.1] - 2026-06-14

### Fixed
- Monitor-token API auth (`GET /v1/models`) and the Prometheus `/metrics` endpoint now read their config from `api.monitoring` / `api.prometheus`, matching where the config editor writes them and where `config.yaml` nests them. Previously these two read sites still looked at the removed top-level `monitoring` / `prometheus` keys, so the monitor token (e.g. `kuma`) was rejected with 401 and `/metrics` auth was misread. `config_watcher` restart-detection was also updated to the `api.prometheus.*` paths.

## [1.16.0] - 2026-06-13

### Added
- New **Usage** page (`/usage`) accessible to all logged-in users, showing their own requests, tokens, cost, model popularity, and heatmap. Admins see their own usage by default with a "Show all users" checkbox to view system-wide data, and a "Last Active" stat when viewing a specific user. User-growth charts (new users, cumulative) appear only in the all-users view.
- Users page: added a bar-chart button per user that opens the Usage page filtered to that user.
- Renamed the admin "Analytics" nav entry to "Usage" and moved it to the main nav for all users.

### Changed
- Config editor: `prometheus` and `monitoring` sections shown as sub-cards of the `API` section; sidebar no longer shows them as separate entries.
- Config editor Models: inactive models are sorted to the bottom of the model dropdown.
- `prometheus` and `monitoring` config keys live at the **top level** of `config.yaml` (not nested under `api`). This was always the intended structure; code now correctly reads them from the top level.

### Fixed
- Config editor no longer warns about "unrecognized fields" when clearing a known field (e.g. `announcement`); the dialog now only fires for top-level keys the editor has no UI for.
- Config editor uses `shutil.copyfile` instead of `shutil.move` to avoid `Operation not permitted` errors when `/tmp` and the config file are on different filesystems.
- Group membership rules now require **all** conditions to match (AND), not just any one (OR); previously a user could be placed in a group by matching only the IdP rule without matching the required affiliation.
- Web chat streaming (`send_message_stream`) now releases its database connection before the LLM call, matching the API path. Previously it held a connection with an open transaction for the entire stream, leaking connections (`idle in transaction`) and exhausting the pool under load.
- Prometheus `/metrics` endpoint and monitor-token API auth now correctly read config from the top-level `prometheus`/`monitoring` keys instead of the non-existent `api.prometheus`/`api.monitoring` path.

## [1.15.2] - 2026-06-13

### Fixed
- Config editor now uses `/tmp` for the temp file during save instead of writing `.tmp` next to the config, fixing `Permission denied` errors when the config directory is root-owned.
- Config editor detects when the config file is not writable and shows a read-only banner with the Save button disabled, instead of failing with a cryptic error after editing.

## [1.15.1] - 2026-06-13

### Fixed
- `_RESTART_REQUIRED` alias added to `config_watcher.py` so tests can import the private name alongside the public `RESTART_REQUIRED` used by the admin config editor.
- `test_database_url_change_warns` updated to use the `app.database` block (replacing the removed `app.database_url` key); `test_restart_keys_covered` updated to assert `("app", "database")` instead of `("app", "database_url")`.

## [1.15.0] - 2026-06-13

### ⚠ Migration Required

- **`app.database_url` and `app.db_pool` have been replaced by a single `app.database` block.** The app will not start without this change. Update your `config.yaml`:

  ```yaml
  # Before
  app:
    database_url: postgresql://user:pass@host/db
    db_pool:
      pool_size: 20
      max_overflow: 30
      ...

  # After
  app:
    database:
      url: postgresql://user:pass@host/db
      pool_size: 20
      max_overflow: 60
      ...
  ```

### Added
- Admin config editor at `/admin/config`: a browser-based YAML editor accessible only to admins. Supports all config sections (app, OAuth2, groups, clients, models, etc.) with live forms, save/reset, and atomic file writes. Linked in the navbar after Analytics for all themes.
- Config editor Models section: "Update" button fetches live metadata from the model's endpoints (context window, max output tokens) and models.dev (knowledge cutoff, reasoning support, modalities) and applies the changes to the form without saving. Toast shows friendly field names (e.g. "context size", "cutoff"). models.dev is cached in-process for 10 minutes so repeated clicks don't re-fetch it.
- Config editor Models section: "Update All" button runs the same sync across every model sequentially, showing a progress counter and a summary toast when done.

### Fixed
- Connection pool exhaustion under concurrent API load: `_do_chat` and `completions` now extract all needed scalar values from ORM objects and call `db.session.remove()` before the LLM call, so pool connections are not held during long upstream requests or streaming responses.
- Announcement banner dismiss key now uses a hash of the full HTML content instead of stripped text, so changing only a URL inside a link correctly shows the updated announcement.

### Changed
- Config editor Admins section: the current user's own email row has its remove button disabled with a Bootstrap tooltip explaining why, preventing self-removal from the admin list.
- Config editor Chat section: Allowed Extensions field now accepts whitespace-separated values (spaces, tabs, or newlines), so extensions can be grouped on one line or many.
- Config editor: moved Admins section to appear after OAuth2 in the sidebar. Section order in the sidebar now also controls the key order in the saved `config.yaml`.
- Config schema: `app.database_url` and `app.db_pool.*` merged into a single `app.database` block (`url`, `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`, `pool_pre_ping`). Update your `config.yaml` accordingly.
- Config editor: saving now detects any YAML fields not recognized by the editor. If any exist, a modal lists them and asks for confirmation before they are permanently removed.

## [1.14.0] - 2026-06-11

### Added
- `api.consent` config flag (default: `true`). Set to `false` to exempt API requests from the graylist model-consent requirement, allowing existing API integrations to keep working while the acknowledgment rollout is in progress. Hot-reloadable.
- `app.graylist_default_notice` config key: a fallback notice shown for any graylisted model that has no per-model notice set. Hot-reloadable.
- Chat page: yellow warning icon (⚠) next to the model picker when a graylisted model has already been consented. Hover shows a tooltip; click opens a Bootstrap popover with the notice text and acknowledgment timestamp, auto-dismissing after 5 seconds (pauses on hover).
- Profile page: "Consented" access cell is now a clickable badge that shows the same acknowledgment popover (notice + timestamp) on click.

## [1.13.0] - 2026-06-06

### Removed
- Conversations are now always permanently deleted; the `chat.remove` config key and soft-delete (hide) mode have been removed. Token usage is preserved in request logs and usage stats regardless of conversation deletion.
- Helm chart: migration job removed — migrations already run in the container entrypoint before uvicorn starts, making the job redundant.

### Added
- `/healthz` endpoint that returns 200 when the database is reachable, 503 otherwise; used by Helm chart startup/liveness/readiness probes instead of `/v1/models` which requires authentication.
- Helm chart: `wait-for-db` init container in the main deployment to prevent crash-looping before PostgreSQL is ready.
- Docker image: non-root `lumen` user (UID 1000) and `UV_NO_CACHE=1` to fix permission errors when running as non-root.
- CI: push Docker image to `ghcr.io/ncsa/lumen` in addition to Docker Hub.
- Helm chart: `UV_CACHE_DIR=/tmp/uv-cache` env var in deployment so uv cache is writable when running as non-root.
- Helm chart: model fields `supports_reasoning`, `input_modalities`, `output_modalities`, `knowledge_cutoff`, and `url` now rendered into `config.yaml` via the chart template.
- Dependency: `flask-limiter[redis]` extra so the `redis` package is installed and Redis-backed rate limiting works.
- Helm chart: `config.name` and `config.tagline` values (defaulting to "Lumen" / "Illuminating AI") rendered into `app.name` / `app.tagline` in `config.yaml`.
- Helm chart: `config.announcement` value rendered into `app.announcement` in `config.yaml`.
- Helm chart: `config.emailThemes` map rendered into `app.email_themes` in `config.yaml`.
- Helm chart: `config.logs.level/access/model` values rendered into `app.logs` in `config.yaml`.
- Helm chart: `oauth2.params` map rendered into `oauth2.params` in `config.yaml` (e.g. `idphint`, `skin` for CILogon).
- Helm chart: model name pattern in `values.schema.json` updated to allow dots and underscores in addition to hyphens.
- Helm chart: Redis Deployment uses `strategy: Recreate` to prevent two pods mounting the same RWO PVC during upgrades.
- Helm chart: Redis pod `securityContext` includes `fsGroup: 999` so the PVC data directory is writable by the non-root Redis user.

### Fixed
- Migration: `ix_messages_conversation_id` index creation in `z0a1b2c3d4e5` now uses `if_not_exists=True` to avoid failure on databases where it was already created by an earlier migration.
- Helm chart: migration job missing `LUMEN_SECRET_KEY` env var, causing app factory to fail during `flask db upgrade`.
- Helm chart: migration job moved from `pre-install` to `post-install` hook so PostgreSQL exists before it runs.
- Helm chart: `runAsUser: 1000` added to migration and lumen containers to satisfy `runAsNonRoot`.
- Helm chart: `chart/Chart.yaml` version and appVersion updated to `1.12.0` to match the application.
- Helm chart: ingress template passes through `ingress.className` and `ingress.annotations` so cert-manager and Traefik annotations are applied correctly.

### Changed
- Helm chart: default image repository changed from `ghcr.io/ncsa/lumen` to `ncsa/lumen` (Docker Hub).
- Helm chart: default TimescaleDB image updated to `timescale/timescaledb:2.27.2-pg17`.

## [1.12.0] - 2026-05-21

### Added
- Announcement banner can now be dismissed by clicking the ✕ button; dismissed state is stored in `localStorage` keyed by message content so a new message re-shows the banner automatically.
- `app.email_themes` in `config.yaml` maps email patterns to theme names (e.g. `"@uic.edu": uic`); domain suffixes (starting with `@`) and exact addresses are supported. Takes precedence over `app.theme`.

## [1.11.2] - 2026-05-17

### Fixed
- `last_refill_at`: standardize on naive UTC (`TIMESTAMP WITHOUT TIME ZONE`) matching all other `DateTime` columns; removes tz-aware/naive comparison hazard in `token_refill.py`
- `subtract_coins`: when balance is too low to cover a request cost, the balance is now zeroed out so subsequent requests are blocked rather than silently served for free
- `subtract_coins`: creates an `EntityBalance` row on first API use if none exists, preventing a silent no-op deduction for new API users
- `get_coin_balance`: removed the side-effect of creating an `EntityBalance` row; returns `starting_coins` when no row exists without mutating the DB
- `GET /v1/models`: replaced per-model `get_effective_limit` calls (N+1 queries) with a single `bulk_model_access_info` + `get_pool_limit` call
- `chat_upload`: added `@limiter.limit` rate limiting (was the only chat endpoint without it)
- `refill_coin_balances`: use fractional elapsed hours instead of truncating to whole hours, preventing permanent coin loss when the refill thread fires between hour boundaries
- `refill_coin_balances`: push the "overdue" filter into the SQL query instead of loading all balances into Python first
- `sync_user_from_yaml`: invalidate the `_nav` session cache on every login so permission changes in `config.yaml` take effect on next login
- Health checker: added `timeout=5.0` to the `openai.OpenAI` client so a slow endpoint cannot block all health checks
- Admin analytics routes: replaced f-string SQL interpolation of the bucket interval with a bound `CAST(:bucket AS INTERVAL)` parameter
- `_reconcile_endpoints`: use `next(..., None)` with a guard instead of bare `next(...)` to avoid `StopIteration` on concurrent config reload
- `admin_required`: non-API browser requests now receive an HTML 403 page instead of a raw JSON error
- `list_conversations`: added `limit` (default 50, max 200) and `before` cursor pagination; frontend shows a "Load more…" button when additional conversations exist
- `list_conversations`: cursor now uses a composite `(updated_at DESC, id DESC)` key to prevent conversations with identical timestamps from being silently skipped on page boundaries
- `list_conversations`: `before` cursor lookup now filters by `entity_id` to prevent timestamp probing of other users' conversations
- `list_conversations`: malformed `?limit=` values now fall back to the default instead of returning 500
- `reset_user_tokens`: reset now restores `starting_coins` rather than `max(starting_coins, max_coins)`, which was incorrectly granting more than the starting allocation
- `/v1/completions`: requests with `stream: true` now return a 400 error instead of silently ignoring the flag and returning non-streaming JSON
- `/v1/chat/completions` and `/v1/completions`: guard against upstreams that return `usage=None` or `choices=[]` (content-filtered responses) instead of crashing with `AttributeError`/`IndexError`
- `chat_upload`: PDF parse errors no longer leak internal exception details to the client; the exception is logged server-side and a generic message is returned
- `_resolve_single_access`: extracted shared access-resolution helper used by both `get_model_access_status` and `bulk_model_access_info`, eliminating duplicated precedence logic
- `ModelConfig.endpoints`, `ModelConfig.stats`, `Entity.api_keys`, `Entity.model_stats`: migrated from deprecated `lazy="dynamic"` to `lazy="select"`
- `_reconcile_endpoints`: build a `{url: endpoint}` dict once instead of iterating the endpoints collection O(n) times per endpoint
- `_deactivate_removed_models`: replaced Python-side filtering with a SQL `WHERE model_name NOT IN (...)` query and bulk `DELETE`/`UPDATE` statements; also fixed a bug where an empty yaml models list left all existing models active
- `refill_coin_balances`: normalize `now` to naive UTC at function entry to prevent `TypeError` when mixing timezone-aware and timezone-naive datetimes across database backends
- Added `*.dump` to `.gitignore` to prevent accidental commit of database dump files

## [1.11.1] - 2026-05-17

### Fixed
- Help sidebar no longer shows the developer-only Architecture and Database Schema pages
- Release link in the help sidebar no longer produces a double `v` prefix in the URL (e.g. `vv1.11.0` → `v1.11.0`)

## [1.11.0] - 2026-05-17

### Fixed
- Migration `y9z0a1b2c3d4`: use composite `PRIMARY KEY (id, time)` on PostgreSQL so TimescaleDB's requirement that the partition column be part of the primary key is satisfied

### Added
- Helm chart at `chart/` for deploying Lumen on Kubernetes; includes bundled PostgreSQL (TimescaleDB) and Redis, standard Ingress and Gateway API HTTPRoute, database migration pre-upgrade Job, and optional in-cluster vLLM/SGLang model inference servers
- Model storage PVCs default to `ReadWriteMany` access mode to support multi-replica deployments sharing a single volume
- `storage.prefetch` flag per model: when `true`, a `pre-install,pre-upgrade` Helm hook Job downloads model weights onto the PVC before any inference pod starts, eliminating concurrent download races

## [1.10.0] - 2026-05-17

### Added
- Chat assistant messages now show the model name next to the ⓘ icon in the message metadata row; thinking tokens are hidden when zero

### Changed
- Removed `.replace(tzinfo=None)` from `RequestLog.time` comparisons in `models_page/routes.py` and `admin/routes.py`; these TIMESTAMPTZ comparisons now use timezone-aware datetimes throughout
- Graylist model consent now uses a shared modal dialog (`_graylist_modal.html` + `graylist-consent.js`) on the chat, profile, and model detail pages instead of separate implementations; the old form-POST `models_page.model_consent` route has been removed in favour of the JSON `/profile/consent/<model>` endpoint
- Chat page: accepting a graylist model via the consent dialog now removes the warning triangle from the model picker and hides the banner without a page reload
- Profile page: accepting a graylist model via the consent dialog now updates the access badge in-place without a page reload
- Replaced bare integer HTTP status codes with `HTTPStatus` constants across all blueprint files (`api`, `auth`, `chat`, `clients`, `profile`, `admin`)
- Renamed the "Usage" page to "Profile": URL changed from `/usage` to `/profile`, nav link updated to "Profile" across all themes, and the admin per-user route moved from `/admin/users/<id>/usage` to `/admin/users/<id>/profile`
- Decomposed `sync_user_from_yaml` (complexity 46) in `auth/routes.py` into four focused helpers: `_desired_groups_from_config`, `_groups_from_userinfo_rules`, `_reconcile_group_memberships`, and `_apply_user_model_overrides`
- `datetime.utcnow()` (deprecated in Python 3.12) replaced with `datetime.now(timezone.utc).replace(tzinfo=None)` in `chat/routes.py` and `api/routes.py`
- `_watcher` exception handler now uses `logger.exception` to preserve stack traces
- Mid-file imports in `profile/routes.py` moved to top of file
- Extracted `_build_model_access_list(entity_id, usage_by_model)` helper in `profile/routes.py`; eliminates ~15-line duplicated loop previously copied across `profile`, `admin`, and `clients` blueprints
- Extracted `_require_client_access(entity_id, sid)` helper in `clients/routes.py`; eliminates duplicated auth guard across four route handlers
- Deferred import of profile helpers in `admin/routes.py` moved to module-level (rule: deferred imports only inside `create_app`)
- `inject_nav` context processor caches `is_admin` and client membership in the Flask session, eliminating 3 DB queries per request after the first
- Extracted `apply_hot_config(app, yaml_data)` into `config_watcher.py`; `create_app` and `_watcher` now share one implementation of hot-reloadable settings
- `completions()` API endpoint now uses shared `_preflight()` helper instead of duplicating model-lookup / budget-check / endpoint-selection from `_do_chat`
- `deduct_coins` one-line wrapper removed; all callers updated to call `subtract_coins` directly
- f-string log calls in `commands.py` converted to `%s`-style lazy interpolation
- `get_pool_limit` now returns a `PoolLimit` named tuple (`max_coins`, `refresh_coins`, `starting_coins`) instead of a plain tuple
- Decomposed `_get_profile_data` (complexity 40) in `profile/routes.py` into three focused helpers: `_fetch_chat_stats`, `_build_model_usage`, and `_build_coin_pool`
- Decomposed `sync_models_from_yaml` (complexity 40) in `commands.py` into three focused helpers: `_apply_model_fields`, `_reconcile_endpoints`, and `_deactivate_removed_models`
- Theme-switching logic extracted into `_apply_theme()` in `config_watcher.py`, called from both startup and the hot-reload watcher
- Docs: corrected access control evaluation order (group defaults resolve before entity default; final fallback is allow not deny)

### Fixed
- `refill_coin_balances` now uses a timezone-aware `datetime.now(timezone.utc)` for `now` and compares directly against `last_refill_at` without stripping timezone info; the previous naive/aware mismatch could silently break on non-UTC PostgreSQL sessions and raise `TypeError` once SQLAlchemy returns an aware value
- Added `tests/unit/test_migrations.py` with a test that asserts the Alembic migration graph has exactly one head, catching unmerged migration branches before they reach review
- `token_refill`: fixed `TypeError` when subtracting `last_refill_at` from `now` after the column was migrated to `DateTime(timezone=True)`; both comparison sites now strip `tzinfo` before arithmetic
- `send_message_stream` now wraps the OpenAI client in a `with` statement, ensuring the SSL context and socket are closed after each chat request
- `entity_balances.last_refill_at` is now written as a timezone-aware datetime matching the `TIMESTAMPTZ` column type, preventing potential `TypeError` on arithmetic with timezone-aware values returned by SQLAlchemy
- `request_logs.time` is now written as a timezone-aware datetime matching the `DateTime(timezone=True)` column declaration
- `update_stats` now uses SQLAlchemy savepoints (`begin_nested`) for the concurrent-seed INSERT, so an `IntegrityError` from a racing first request no longer rolls back the entire session and discards the preceding coin deduction
- `subtract_coins` now logs a warning when the balance is already exhausted and the UPDATE affects 0 rows, making silent no-charge events observable in logs
- `check_coin_budget` no longer calls `get_effective_limit` twice per request; the resolved limit from the first call is reused for the balance check
- `/v1/models` and `/v1/models/<id>` now pre-fetch all endpoints in a single query instead of issuing one SELECT per model (eliminates N+1 on the hot models endpoint)
- `/models` page now resolves model access for all models in a fixed number of queries via `bulk_model_access_info`, replacing per-model `get_model_access_status` calls
- Profile usage tab now resolves model access and endpoint health in bulk, replacing per-model `get_model_access` and lazy-loaded `get_model_status` calls
- `/v1/completions` now records `endpoint_id` and `duration` in `RequestLog`, matching the `/v1/chat/completions` behaviour
- Background threads in `health.py` and `token_refill.py` now log exceptions with `logger.exception()` instead of silently swallowing them
- Health checker joins `ModelConfig.model_name` upfront instead of lazy-loading `ep.model_config` inside the loop; accessing the backref on a `lazy="dynamic"` + `delete-orphan` relationship caused `StaleDataError` on commit
- `refill_coin_balances` now bulk-loads `EntityLimit`, `GroupMember`, and `GroupLimit` rows before the loop, eliminating N+1 queries per entity
- `_build_model_access_list` and `chat_page` now bulk-resolve model access status, consents, and endpoint health in a fixed number of queries, replacing N+1 per-model queries
- Added `bulk_model_access_info()` helper to `services/llm.py` for efficient entity-wide access resolution
- Model endpoint lists are now pre-fetched in bulk on the profile page, eliminating one lazy SELECT per model
- `APP_ANNOUNCEMENT` in config.yaml is now sanitized with `bleach.clean()` before being marked safe, preventing HTML/JS injection from config-level input
- `assert` guards before f-string SQL interpolation in analytics routes replaced with explicit `if … abort(BAD_REQUEST)` — `assert` is disabled under Python `-O`
- `_md_filter` Jinja2 filter documented as operator-only; output must never be applied to user-supplied content
- Removed misleading `SECRET_KEY` env var read from `config.py`; it was always overwritten at runtime by `LUMEN_SECRET_KEY`, silently ignoring operator intent
- Monitor token comparison now uses `hmac.compare_digest()` to prevent timing side-channel attacks
- `/chat/stream` now enforces a 500-message count limit and 500,000-character total payload limit per request
- Fixed race condition in `update_stats`: seed INSERT for new `(entity, model, source)` triples now wrapped in `try/except IntegrityError` so concurrent first-requests no longer cause an unhandled 500
- `request_logs` now uses a surrogate `BIGINT` autoincrement PK; `time` is kept as a regular indexed non-unique column, eliminating timestamp collision between concurrent workers
- `ModelStat` and `EntityStat` counters now use SQL-level atomic increments instead of ORM read-modify-write, preventing lost updates under concurrent requests
- `subtract_coins` now uses a single atomic `UPDATE ... WHERE coins_left >= cost` so concurrent requests cannot both deduct from an insufficient balance
- `request_logs` inserts now add 0–999 µs jitter to the timestamp PK to prevent collisions under concurrent requests

### Database
- Added migration `z1b2c3d4e5f6` to enforce `NOT NULL` on `entity_balances.coins_left` and `entity_balances.last_refill_at`, convert `last_refill_at` to `TIMESTAMPTZ`, and enforce `NOT NULL` on `api_keys.key_hash`
- Added merge migration `z2a3b4c5d6e7` to reconcile four divergent heads
- Added index on `model_endpoints.model_config_id` to avoid full table scans on every endpoint lookup
- Added index on `entity_managers.client_entity_id` to support efficient lookups by client when listing managers
- `entity_balances.coins_left` and `entity_balances.last_refill_at` are now `NOT NULL`; `last_refill_at` changed to `TIMESTAMP WITH TIME ZONE`
- `api_keys.key_hash` is now `NOT NULL` (legacy plaintext-to-hash migration is complete)
- Analytics API endpoints return empty results instead of `OperationalError` when running on SQLite
- Dropped deprecated `model_configs.max_input_tokens` column; use `context_window` instead
- Added `CHECK (entity_type IN ('user', 'client'))` constraint on `entities` table
- API key deletion now hard-deletes the row instead of soft-deactivating it
- Added composite index `ix_conversations_entity_hidden_updated` on `conversations(entity_id, hidden, updated_at)` to speed up `list_conversations` queries
- Added `ix_messages_conversation_id` index to `messages.conversation_id`
- Added FK indexes on `group_members.entity_id`, `entity_model_access.entity_id`, `group_model_access.group_id`, `model_stats.(entity_id, model_config_id)`, `request_logs.(entity_id, model_config_id)`, and `api_keys.entity_id`

### Accessibility
- Fixed `colspan` on API keys table loading row from 7 to 6 to match the actual column count (WCAG 1.3.1)
- Info-icon `ⓘ` spans now respond to Enter/Space keyboard events to toggle the Bootstrap Popover (WCAG 2.1.1)
- Active/inactive status icons `✓`/`✗` wrapped in `<span role="img" aria-label="...">` in clients and admin/users tables (WCAG 1.1.1)
- Autocomplete manager listbox now handles `Home`/`End` keys to jump to first/last suggestion (WCAG 2.1.1)
- Sidebar toggle button `aria-label` now updates to "Show sidebar" / "Hide sidebar" on each click (WCAG 4.1.2)
- All sort-header `<th>` elements now carry `scope="col"` for unambiguous screen-reader column association (WCAG 1.3.1)
- Removed redundant `aria-label` from `#period-select` in analytics; the visible `<label>` is sufficient (WCAG 2.5.3)
- Added fallback text content inside all five `<canvas>` chart elements for assistive technology that does not expose `aria-label` on canvas (WCAG 1.1.1)
- Wrapped `✓`/`—` capability flags in `model_detail.html` with `<span role="img" aria-label="...">` (WCAG 1.1.1)
- Modal close buttons now carry context-specific `aria-label` values ("Close New API Key dialog", "Close Access Acknowledgment dialog") (WCAG 4.1.2)
- `overflow:hidden` on main content wrapper changed to `overflow:auto` to prevent clipping at browser zoom (WCAG 1.4.10)
- Removed `overflow-y:hidden` from KaTeX display blocks — tall math equations no longer clip at zoom (WCAG 1.4.4)
- Sortable table `<th>` elements now have `tabindex="0"`, Enter/Space keydown handlers, `aria-sort` attributes, and bold active arrows so sort state is conveyed beyond colour alone (WCAG 1.4.1, 2.1.1, 4.1.2) — affects clients, client detail, profile, and admin users tables
- SkipTo.js moved from Illinois theme `head_extras.html` into `base.html` and `landing.html` so all themes provide skip navigation (WCAG 2.4.1)
- Inner `<main>` in `help.html` changed to `<section>` to eliminate duplicate `<main>` landmark (WCAG 1.3.1)
- Attachment error dismiss now briefly emits an SR-only "dismissed" message before clearing the `aria-live` region (WCAG 4.1.3)
- `.conv-item:focus-within` now reveals the conversation remove button for keyboard users (WCAG 2.1.1)
- Admin users search input now has `aria-label="Search users by name or email"` (WCAG 4.1.2)
- `aria-selected` on `role="listitem"` conversation items replaced with `aria-current` (valid on any role) (WCAG 4.1.2)
- Empty heatmap day column header now contains a visually-hidden "Day" label in both static HTML and the JS-rendered header row (WCAG 1.3.1)

### Security
- File upload responses now return the sanitized filename instead of the raw browser-supplied name, preventing unsanitized input from reaching client-side DOM rendering paths
- Non-streaming `/v1/chat/completions` and `/v1/completions` error responses now return a generic message; upstream exception details are logged server-side only
- Application now refuses to start if `DEV_USER` is set while running in production mode (`SESSION_COOKIE_SECURE=True`), preventing the dev-login bypass from being reachable on public deployments
- Streaming error responses in `/v1/chat/completions` and `/chat/stream` now return a generic message; upstream exception details (which could include API keys or host names) are logged server-side only
- Analytics `period` parameter in user-growth endpoints is now validated against the allowed set before use; `trunc` values derived from it are also guarded with an explicit allowlist check
- Fixed Prometheus `/metrics` token comparison to use `hmac.compare_digest()` preventing timing side-channel attacks
- Fixed `model_readme` URL check to use `urlparse` hostname validation, preventing SSRF via credential-injection URLs
- `_md_filter` Jinja filter documented as operator-only; never apply to user-supplied content
- Upload filenames are sanitized with `werkzeug.utils.secure_filename` before extension extraction and display
- `_rates_cache` update in the API blueprint is now protected by a `threading.Lock`, eliminating a thundering-herd race under burst traffic
- Fixed path traversal vulnerability in `/help/img/` route: replaced `send_file` with `send_from_directory` which rejects `../` sequences
- Added HTTP security response headers (`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Strict-Transport-Security`) on all responses
- Added session cookie security flags (`Secure`, `HttpOnly`, `SameSite=Lax`, 24h lifetime)
- Changed `SECRET_KEY` fallback in `config.py` from known default to empty string so misconfigured deployments fail loudly
- Added localhost-only guard to `/devlogin` when not running in debug mode
- Prometheus with no token configured now logs an error at startup and disables the `/metrics` endpoint (returns 404) instead of serving unauthenticated metrics
- Added startup warning when rate limiting uses in-memory storage (ineffective under multi-worker deployments)
- Added allowlist assertions before f-string SQL interpolation in analytics routes
- Documented `app.announcement` in `config.yaml.example` as trusted operator HTML (not escaped by Jinja2)

## [1.9.3] - 2026-05-11

### Fixed
- vLLM models that emit `delta.reasoning` (instead of `delta.reasoning_content`) now have their chain-of-thought captured and displayed as thinking

### Added
- Help page sidebar now shows the app name (linked to GitHub), version, and git commit at the bottom; version and commit are baked into the Docker image at build time via `APP_VERSION` and `GIT_COMMIT` build args; local dev shows `develop` / `N/A`
- Thinking/reasoning content from the model is now saved to the database and shown again when reloading a past conversation (collapsed "Thought" block)
- Token popup (ⓘ) now shows thinking tokens and text output tokens as separate values for reasoning models
- Site-wide announcement banner below the navbar, configured via `app.announcement` in config.yaml; supports HTML; colors are theme-configurable (`banner_bg_color`, `banner_text_color`) with a pastel yellow default; hot-reloads without a restart

## [1.9.2] - 2026-05-10

### Added
- Test coverage: spec-compliant OpenAI model mock (adds required `created`, `object`, `owned_by` fields); new negative/else-branch tests for entity `model_access_default`, `subtract_coins` with no balance row, group unlimited pool, `EntityBalance` with null `last_refill_at`, `sync_groups_from_yaml` GroupLimit deletion and unknown-model skip, `sync_clients_from_yaml` no-config skip, `sync_user_from_yaml` non-matching rules / `equals` predicate / limit removal / model whitelist, missing-`messages` 400 on `/v1/chat/completions` and `/chat/stream`, `devlogin` 403 without `DEV_USER`, and OpenAI response field validation on `/v1/models`

### Fixed
- Chat message timestamps showed "Invalid Date" because `formatTimestamp` appended a second `Z` to timestamps already ending in `Z` from the backend's `strftime` format
- `list_conversations` in `chat/routes.py` used the deprecated `Conversation.query` pattern (banned by CLAUDE.md); replaced with `db.session.execute(select(...))`
- New `EntityBalance` rows were created without `last_refill_at`, leaving them permanently excluded from the coin refill query (`WHERE last_refill_at IS NOT NULL`); both creation sites (`get_coin_balance` in `llm.py` and `sync_user_from_yaml` in `auth/routes.py`) now stamp the current UTC time so newly created balances are picked up by the refiller after one hour

### Changed
- Coin budget resolution: a per-user `EntityLimit` now always wins over group `GroupLimit`s, consistent with how model access works (entity-level rules override group defaults). Previously the highest `max_coins` across user and all groups won, making it impossible to cap a user below their group's budget without removing them from the group.
- README: updated intro from "chat portal" to "AI gateway" to reflect API proxy capability; added key features for uploads, service accounts, theming, analytics, and Prometheus; added missing config sections (theme, chat.upload, clients, monitoring, prometheus); expanded clients section with full explanation of coin pools, managers, API usage, and model access; added `supports_function_calling` to model config reference; fixed `url` field description; documented coin budget resolution order
- config.yaml.example: corrected built-in theme list to `default, illinois, uic, uis`
- docs/dbschema.md: added "Coin Budget Resolution Order" section

## [1.9.1] - 2026-05-09

### Fixed
- Removed SkipTo.js accessibility test check — SkipTo.js is illinois-theme-only and the check was failing for other themes

## [1.9.0] - 2026-05-09

### Added
- Theme system: branding (header, footer, logo, colors, CSS/JS) is defined per-theme in `themes/<name>/`. Set `app.theme` in `config.yaml` to switch themes; changes hot-reload within 5 seconds without a restart. Each theme provides `theme.yaml`, `templates/theme/` partials (header, footer, page_open/close, head_extras), and an optional `static/` folder. Built-in themes: `illinois` (default, Illinois Toolkit web components), `default` (plain Bootstrap), `uic` (University of Illinois Chicago — uic.edu branding with official SVG logos, 80px navbar, multi-column footer), and `uis` (University of Illinois Springfield — uis.edu branding with official wordmark, white/navy header, 1200px container, three-column footer with campus/site links and social icons). Chat bubble colors follow the active theme via `--bubble-user` / `--bubble-assistant` CSS variables.
- "About Illinois Computes" and "Feedback & Support" sections added to the Introduction help page, crediting Illinois Computes and NCSA, with links to computes.illinois.edu, the NCSA support email, and the GitHub issue tracker
- CSRF protection via Flask-WTF: the model consent form is now protected; the `/v1/` API blueprint is exempt; JavaScript fetch calls (upload, delete conversation) send `X-CSRFToken` header
- `todo.md` with detailed follow-up items for deferred technical debt (bulk access resolution, admin SQL pattern, CSS consolidation, SQLAlchemy modernization)
- Tests for help and usage blueprint routes, covering key management, consent flow, coin pool, model status, and markdown frontmatter parsing

### Changed
- The `default` theme is now the fallback when no `app.theme` is set in `config.yaml` (previously `illinois`)
- Modernized all SQLAlchemy queries from the legacy `Model.query` and `db.session.query()` APIs to `db.session.execute(select(...))` / `db.first_or_404(stmt)` / `db.session.scalar()` across all production files and test files; updated CLAUDE.md to ban both deprecated patterns
- Consolidated three copies of `_model_status` into `get_model_status()` in `lumen/services/llm.py`

### Fixed
- N+1 queries on `/models` page: model endpoints now fetched in one query and passed to template via `endpoints_map` instead of calling `config.endpoints.all()` per row
- N+1 queries on `/chat` page: healthy endpoint counts now fetched in one GROUP BY query instead of per-model `.count()` calls
- N+1 queries in `list_conversations`: last message per conversation now fetched in a single subquery join instead of one query per conversation
- Datetime timezone in chat JSON responses: `updated_at` and `created_at` now include `Z` suffix so JavaScript interprets them as UTC
- Removed dead `if not mc.active` branch from inner model status function in `_get_usage_data` (unreachable — only accessible/active models are evaluated there)

## [1.8.0] - 2026-05-09

### Added
- `entity_stats` table: pre-aggregated per-entity usage totals (requests, tokens, cost, last\_used\_at) maintained in real-time alongside `model_stats`; eliminates full-table GROUP BY scans on the admin users page, admin users API, and clients listing
- Admin help docs (`docs/admin/`) covering configuration overview, application settings, user groups and access control, clients, and model configuration
- `LUMEN_SECRET_KEY` environment variable to override `app.secret_key` without putting it in `config.yaml`
- Dev server (`run.py`) now watches `docs/nav.json` for changes and hot-reloads automatically
- Clicking a user's name in the admin users list now navigates to a read-only usage view for that user, showing their name, email, and group memberships
- `docs/dbschema.md` — full database schema reference with column descriptions, constraints, ER diagram, and access control evaluation order
- Alembic migration `v2w3x4y5z6a7` adds `COMMENT ON TABLE/COLUMN` to all tables on PostgreSQL
- `dev.sh` now pulls the latest TimescaleDB image before starting the container

### Changed
- Date columns in sortable tables now default to descending order (newest first) when first clicked
- Admin users table redesigned: email column replaced with Joined, Last Used, Coins Left, and Coins Spent; never-active users sort to the bottom; dates displayed in the user's local timezone
- All SQLAlchemy model classes now carry docstrings and `comment=` on every column and table, surfaced as PostgreSQL catalog comments
- Help docs updated: clearer wording for model detail fields, clients section, and coin cost example in introduction
- Existing help doc cross-links audited and corrected
- Coin pool and model access overrides are now config-only; the per-user and per-group edit UI has been removed. Use `config.yaml` groups to manage limits and model access.

### Removed
- Admin Groups page removed from navigation and UI; groups remain config-managed only via `config.yaml`
- Top-level `model_access:` config section removed; use `groups.default.model_access` for site-wide defaults and per-group `model_access` for per-group rules. Alembic migration `u1v2w3x4y5z6` drops the `global_model_access` table.
- Admin `/admin/users/<id>/limits` page removed (coin pool overrides and model access overrides are now config-only)

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
