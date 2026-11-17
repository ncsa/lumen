# AGENTS.md

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

## 5. Application Specific Rules

Following rules are here to help the AI avoid the same mistakes again:
- Run the dev stack with `docker compose up --build --watch` so source changes rebuild/sync into the container.
- Sortable table columns: date and numeric columns default to descending order on first click; text columns default to ascending.
- All times are UTC everywhere in the app and the DB; only convert to the user's local timezone when displaying to them. DB timestamp columns are stored as **naive UTC** (no tzinfo) so values behave identically on SQLite and PostgreSQL. Always get "now" from `lumen.timeutils.utcnow()` (returns naive UTC) — never use `datetime.now(timezone.utc).replace(tzinfo=None)` or `datetime.utcnow()` directly, and use `default=utcnow` on `db.DateTime` columns. (Exception: `request_logs.time` is `timestamptz`/aware because it is a TimescaleDB hypertable partition key.) In templates, emit `<span class="local-datetime" data-utc="{{ dt.strftime('%Y-%m-%dT%H:%M:%SZ') }}"></span>` and let the JS in app.js convert it to local time. Never hardcode "UTC" in displayed timestamps.
- Dependencies are managed with uv, code is run with uv
- Imports at top of file only; except inside Flask app factory (`create_app`) where deferred imports are required to avoid circular imports.
- Styles and colors match University of Illinois, see https://builder3.toolkit.illinois.edu/getting_started/index.html
- When adding variables to config.yaml, make sure they are hot loaded if possible or print a warning
- Track changes in the CHANGELOG.md, if no unreleased section exists, then add it, make sure to not add a new subsection.
- Keep CHANGELOG entries short: one terse bullet per logical change, present tense, user-facing language. Do not write long narrative paragraphs explaining the "why" at length. Where a change has an obvious PR, link it (e.g. `(#123)`), or an issue in GitHub.
- Every GitHub issue worked on must have a CHANGELOG entry that links it as `(#N)`, and every bullet for that issue carries the link. Before finishing, check that each bullet for the issue ends with `(#N)`.
- Adding or adjusting a small test on its own does not warrant a CHANGELOG entry.
- When bumping the version in pyproject.toml, also run `uv lock` to update uv.lock.
- When bumping the version for a release, also update `version` and `appVersion` in chart/Chart.yaml to match.
- The monitor in `scripts/monitor` is versioned in lockstep with Lumen: when bumping the version for a release, set the same version in `scripts/monitor/pyproject.toml` and run `uv lock` in `scripts/monitor` too.
- When cutting a release, first pull the latest dependencies (`uv lock --upgrade`) and then run the full test suite before tagging — upgraded dependencies can introduce breaking changes, so the release must not go out unless the tests pass.
- "The full test suite" always includes the PostgreSQL and Redis tests; a plain `uv run pytest` silently skips them. Start throwaway services and point the suite at them:
  ```bash
  docker run -d --rm --name lumen-test-pg -e POSTGRES_USER=lumen -e POSTGRES_PASSWORD=lumen -e POSTGRES_DB=lumen_ci -p 55433:5432 timescale/timescaledb:2.31.0-pg17
  docker run -d --rm --name lumen-test-redis -p 56380:6379 redis:7
  LUMEN_TEST_POSTGRES_URL=postgresql://lumen:lumen@localhost:55433/lumen_ci LUMEN_TEST_REDIS_URL=redis://localhost:56380/0 uv run pytest -rs
  docker stop lumen-test-pg lumen-test-redis
  ```
  Check the `-rs` skip summary: no test may be skipped for a missing `LUMEN_TEST_POSTGRES_URL` or `LUMEN_TEST_REDIS_URL`.
- When uploading the changelog for a release, add Contributors at the bottom as a list with a link to their github profile, icon and name.
- For local testing without OAuth or a real LLM: set `app.dev_user` in config.yaml and use `uv run dummy` (dummy backend on port 9999). See the "Local Development" section in README.md.
- Keep the help docs in `docs/` up to date: any change to a user-facing screen (chat, profile, usage, models, model detail, projects, admin) must be reflected in the matching page under `docs/guides/`, `docs/models/`, `docs/projects/`, or `docs/admin/`, including re-capturing affected screenshots (see next rule). Schema changes go in `docs/dbschema.md` (see below).
- New documentation added to docs, should also be reflected in mkdocs.yml
- Screenshots for the help docs live in `docs/img/`. Re-capture them with playwright after any UI change that affects chat, usage, models, model detail, or projects pages. Use `CONFIG_YAML=./dev.config.yaml uv run python -c "from lumen import create_app; ..."` with a dev config that has `dev_user` set.
- Every new page (a route that renders HTML) must be added to `ROUTES` in `scripts/responsive_check.py` (with the users who can see it, and to `EXPECTED_STATUS`/`EXPECTED_REDIRECTS` if it does not answer 200 at its own path) and to `PAGES` in `tests/ui/test_responsive_markup.py`. Run the audit for it (`ONLY=<name>`) and make sure it passes at all four sizes before the PR.
- SQLAlchemy: never use `Model.query` or `db.session.query()` — both are deprecated in SQLAlchemy 2.x and emit `LegacyAPIWarning`. Use `db.session.execute(select(Model)...)` for all queries, `.scalars().all()` / `.scalar_one_or_none()` for results, `db.first_or_404(stmt)` for 404 guards, `db.session.get(Model, pk)` for PK lookups, `db.get_or_404(Model, pk)` when a missing row should 404. Model definitions also use 2.x style: declare columns as `name: Mapped[T] = mapped_column(...)` (use `Mapped[Optional[T]]` for nullable columns) and relationships as `Mapped[list["X"]]`/`Mapped[Optional["X"]]`/`DynamicMapped["X"]` — never the legacy `db.Column(...)`/`db.relationship(...)` form. Keep the explicit type in `mapped_column` (e.g. `db.BigInteger`, `db.Numeric(12, 6)`, `db.Text`) so it is not lost to annotation inference.
- Warnings: both test suites run with `filterwarnings = ["error"]`, so any warning fails the test that raised it. Fix a warning at its source; never silence it with a broad filter. A third-party warning that cannot be fixed gets a narrow `ignore:<message>:<Category>:<module>` entry in `pyproject.toml` with a comment linking the upstream issue. Never use `datetime.utcnow()` or Flask-SQLAlchemy's `get_engine()`, and never bind raw `datetime` values to SQLite in raw SQL (bind ISO strings). Close everything a test opens: `engine.dispose()` for engines it creates, `contextlib.closing()` for `sqlite3` connections (their own `with` only commits), `with` for files, and `with client.get(...) as resp:` for responses that serve a file. Find a `ResourceWarning: unclosed ...` reported in an unrelated test with `PYTHONTRACEMALLOC=20`.
- Coverage: every file measured in `lumen/` and `scripts/monitor/monitor/` must stay at or above 90% line coverage. CI enforces this per file with `scripts/check_coverage.py`, which `tests/unit/test_check_coverage.py` pins; check locally with `uv run pytest --cov-report=json` (with PG and Redis for Lumen) and `uv run python scripts/check_coverage.py` (from `scripts/monitor`: `uv run pytest --cov-report=json && uv run python ../check_coverage.py`). New code ships with tests. Background-thread loops are tested by running one iteration. `# pragma: no cover` is allowed only with a comment saying why the line is unreachable in-process.
- DB connection hygiene — narrow rule, don't over-apply: ordinary (non-generator) request handlers should keep relying on Flask-SQLAlchemy's implicit per-request teardown to release `db.session` — that mechanism is correct and well-tested; don't add defensive `db.session.remove()`/commit/rollback calls to plain views that don't need them. The one rule that is absolute: **no DB session and no Flask context (app or request) may span a `yield`, in any generator.** This has bitten production twice:
  - A generator that interleaves DB calls with multiple `yield`s and is driven by something other than Flask's request/response cycle (e.g. a Prometheus `Collector.collect()` consumed by `generate_latest()`) must release its session explicitly in a `finally` — `/metrics`' `LumenDBCollector.collect()` leaked a connection idle-in-transaction until Postgres killed it, surfacing as `server closed the connection unexpectedly` on an unrelated request.
  - A streaming response-body generator must run **context-free**. `stream_with_context` is **banned** (enforced by `tests/unit/test_no_stream_with_context.py`): it re-pushes the request's app context onto whichever WSGI worker thread iterates the body, and a client disconnect abandons the generator so the push is never undone — the thread is permanently poisoned, every later request on it reuses the stuck context, and its session teardown never runs (one leaked idle-in-transaction connection per poisoned thread; Flask swallows the error so nothing is logged). Instead: capture `app = current_app._get_current_object()` and every needed scalar in the view while the request context is still current, then inside the generator wrap each DB phase in its own short-lived `with app.app_context():` that closes before the next `yield`. See `llm.py`'s `send_message_stream` and the streaming paths in `chat/routes.py`/`api/routes.py` for the template.
- Billing: every billed request goes through `bill_request` in `lumen/services/llm.py`, which writes in one fixed order (`request_logs` → `model_stats` → `entity_stats` → `api_keys` → `entity_balances` → COMMIT) so the per-entity locks are taken last and two billing transactions cannot deadlock. Never call `update_stats`, `subtract_coins` or `_record_api_key_usage` directly from a route, and add any new billing write inside `bill_request` in that order. `tests/routes/test_billing_queries.py` pins the order on every billing path; extend it when adding one. Other writes to `entity_stats` (the conversation counter) go last in their transaction, just before the commit.
- updates to the schema should be reflected in docs/dbschema.md and columns/tables should have comments.
- Every id visible to a user — in URLs, JSON payloads, templates/JS, form fields, CLI arguments and the session cookie — must be the row's opaque `uuid` column (or another existing public unique key such as `model_name`), never an integer primary key: integer ids are sequential and easy to guess. Integer ids stay server-side, for primary keys, foreign keys and joins only. Routes take the id with the `<uuid:...>` converter and resolve it with the helpers in `lumen/models/lookups.py` (`entity_by_uuid_or_404`, `group_by_uuid_or_404`, `key_by_uuid_or_404`). A new table whose rows a user can address gets a `uuid` column like the existing ones (`db.Uuid`, unique, not null, `default=uuid4`, with the external-use-only comment).
- A migration that changes what `flask upgrade` must do — a new post-migration step, or a continuous aggregate created, rebuilt or reordered — must also update `docs/admin/commands.md` (the `upgrade`, `backfill-aggregate` and `check-aggregate` sections) and add an **Upgrade:** note to `CHANGELOG.md` if operators must act. A new `flask` command needs its own section there; `tests/unit/test_commands_documented.py` fails without it.
- Don't use bare integer HTTP codes, use `HTTPStatus` constants.
- updates to config file should be reflected in the helm chart values.yaml and values.schema.json.

## 6. Accessibility (WCAG 2.1 AA)

**All HTML must be WCAG 2.1 AA compliant. This is not optional.**

You must have tests that check the code for compliance, and these tests must pass.

When writing or modifying HTML/JS:
- **ARIA labels:** Every interactive element needs an accessible name. Icon-only buttons (`✕`, `☰`, `←`, `ⓘ`) require `aria-label`. Never rely on `title` alone.
- **Form labels:** Every `<input>`, `<select>`, and `<textarea>` must have an associated `<label>` with a `for`/`id` pair. Use `class="visually-hidden"` if the label should not be visible. Placeholder text is not a label substitute.
- **Modals:** Add `aria-labelledby` pointing to the modal title's `id`. Close buttons need `aria-label="Close"`.
- **Dynamic content:** Use `aria-live="polite"` (or `role="log"`) for areas updated via JS (chat messages, status results). Use `role="alert" aria-live="assertive"` for error/success feedback.
- **Keyboard access:** If an element has a click handler, it must also be focusable (`tabindex="0"`) with a `keydown` handler for Enter/Space, or use a native `<button>`/`<a>`. Hover-only visibility must also trigger on `:focus`.
- **Color contrast:** Normal text needs 4.5:1 minimum. Do not use UIUC orange (`#e84a27`) as a background with white text (only 3.0:1). Use `#b5300c` or darker. Verify with a contrast checker.
- **Headings:** Maintain proper hierarchy (h1 → h2 → h3). Never skip from h1 to h5.
- **Tables:** Data tables need `<caption>` (use `class="visually-hidden"` if needed). Empty `<th>` cells for action columns need `<span class="visually-hidden">Actions</span>`.
- **Emoji as content:** Wrap meaningful emoji in `<span role="img" aria-label="description">`.
- **Skip navigation:** SkipTo.js is loaded in `base.html` and `landing.html` from its CDN (`skipto-landmarks-headings.github.io/page-script-5`). Do not remove it.
- **Timed content:** Auto-dismiss timers must be at least 20 seconds and pause on hover/focus (WCAG 2.2.1).
- **Overflow:** Never use `overflow:hidden` on scrollable content containers — use `overflow:auto` to prevent clipping at zoom.
- **Focus management:** After JS changes page content (loading data, deleting items), move focus to a logical target (e.g., input field, action button).
- **Active/selected state:** Never convey state through color alone. Combine with a border, icon, bold, or `aria-selected`/`aria-current`.
