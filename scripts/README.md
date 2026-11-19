# Dev scripts

Helper scripts for local testing and for regenerating the help-doc screenshots.

For a browser example of the OAuth (PKCE) key-request flow, with a reusable JS module, see
[`web-oauth/`](web-oauth/README.md). It needs no Playwright; serve it with `python -m http.server`.

The Anthropic Messages API scripts (`claude.sh`, `anthropic_smoke.py`, `anthropic_toolbot.py`)
need no Playwright either; see [Anthropic Messages API](#anthropic-messages-api) below.

The Python scripts below drive a real browser with Playwright, so install the browser once:

```bash
uv run python -m playwright install chromium
```

Start a local instance first (see the "Local Development" section of the top-level
`README.md`). In one terminal:

```bash
uv run dummy                                   # echo backend on :9999
```

In another, run the app with a dev config that sets `app.dev_user` (an admin):

```bash
CONFIG_YAML=./dev.config.yaml uv run python -c \
  "from lumen import create_app; create_app().run(port=5001, debug=True, threaded=True)"
```

> `threaded=True` matters: chat streams hold a connection open, and a single-threaded
> dev server will block the browser's other requests.

## smoke_test.py

Crawls every reachable in-app link and probes known edge cases (missing models,
projects, users, help pages). Fails if any link 404/500s or renders an unstyled
crash page. Use it after UI or routing changes.

```bash
BASE_URL=http://localhost:5001 uv run python scripts/smoke_test.py
```

## responsive_check.py

Audits every page at phone and tablet sizes (375×667, 768×1024, 1024×768) and at
1280×800, signed in as the dev admin (admin mode on) and as the non-admin demo
user (the landing page signed out). For each page and size it reports horizontal
overflow and names the elements causing it; on chat it also checks that the input
bar is fully visible without scrolling, and at 768 and up that the sidebar and
message pane keep their widths when a long thinking block and answer are added
(with the conversation list empty and with conversations). It writes `report.md` (a pass/fail table)
and full-page screenshots to `responsive-audit/` (git-ignored) and exits 1 if
anything fails, including pages it could not measure properly (an unexpected HTTP
status or redirect, a detail page with nothing to link to, or a chat page without
its input bar). It seeds the same demo data as `screenshots.py`, plus a demo group,
so run it with the **same** `CONFIG_YAML` as the running app.

```bash
CONFIG_YAML=./dev.config.yaml uv run python scripts/responsive_check.py
ONLY=chat,models CONFIG_YAML=./dev.config.yaml uv run python scripts/responsive_check.py
```

Override defaults with `BASE_URL`, `OUTPUT_DIR`, `MODEL`, `CHROME_PATH`, and `ONLY`
(comma-separated page names from the report).

## screenshots.py

Regenerates the screenshots in `docs/img/` (chat, models, usage, projects,
project detail, profile, model detail). The footer and skip-to-content link are cropped, and the
model-detail shot is taken as a non-admin user so admin-only endpoint URLs are not
shown. Run it with the **same** `CONFIG_YAML` as the running app.

```bash
CONFIG_YAML=./dev.config.yaml uv run python scripts/screenshots.py
```

Override defaults with `BASE_URL`, `OUTPUT_DIR`, `MODEL`, and `CHROME_PATH`.

### Polished docs capture

The committed `docs/img/` shots are captured with `screenshot.config.yaml` and
`screenshot_seed.py` (both in this directory) for a production-looking result:
app name **Lumen**, the **illinois** theme, and real HuggingFace model
names/cards, with a week of seeded usage and a curated chat conversation.

Usage analytics only render on **PostgreSQL/TimescaleDB** (the `/api/usage/*`
endpoints short-circuit to empty on SQLite), so capture against Postgres. The
config holds no secrets — pass `LUMEN_SECRET_KEY`, `LUMEN_ENCRYPTION_KEY`, and a
Postgres `DATABASE_URL` via the environment. Model endpoints point at the local
echo backend (`uv run dummy`) so chat renders and models read healthy without a
GPU; the model-detail card is still fetched live from huggingface.co.

```bash
export LUMEN_SECRET_KEY=dev-secret-key-for-screenshots-0001
export LUMEN_ENCRYPTION_KEY=dev-encryption-key-for-screenshots-0001
export DATABASE_URL=postgresql://USER:PASS@HOST:5432/lumen_shots
export CONFIG_YAML=./scripts/screenshot.config.yaml MODEL=qwen2.5-7b-instruct

uv run flask --app "lumen:create_app" db upgrade   # build schema on a fresh DB
uv run python scripts/screenshot_seed.py           # seed usage + a conversation
uv run dummy &                                     # echo backend on :9999
uv run python -c "from lumen import create_app; create_app().run(port=5002)" &
BASE_URL=http://localhost:5002 uv run python scripts/screenshots.py
```

## Anthropic Messages API

These scripts exercise Lumen's Anthropic-compatible Messages API. The
[Anthropic guide](../docs/guides/anthropic.md) covers the client settings they use.

### claude.sh

Runs [Claude Code](https://docs.anthropic.com/en/docs/claude-code) against a Lumen
instance instead of Anthropic's API. It is a starting point to copy and adapt, not a
supported launcher.

Prerequisites: Claude Code is installed, and `LUMEN_BASE_URL` and `LUMEN_API_KEY` are
set (run `lumen.sh` once to log in and store them in your shell rc file, then open a
new shell).

```bash
ANTHROPIC_MODEL=<model> scripts/claude.sh [claude arguments...]
```

`ANTHROPIC_MODEL` is required and must name a model Lumen serves (see `/v1/models`).
`ANTHROPIC_DEFAULT_HAIKU_MODEL`, the model Claude Code uses for background tasks,
is optional and defaults to `ANTHROPIC_MODEL`. The script also sets the `CLAUDE_CODE_*`
and `MAX_THINKING_TOKENS` switches that non-Anthropic models need; the
[Anthropic guide](../docs/guides/anthropic.md) explains each of them. Arguments are
passed through to `claude`.

### anthropic_smoke.py

End-to-end checks of the Messages API against a running Lumen, over real HTTP, with
the clients people actually use: the official `anthropic` Python SDK (non-streaming,
streaming, token counting, errors, tool use) and, when installed, the Claude Agent SDK
and Claude Code. It catches what a real client sends that no hand-written request does.
It needs `anthropic_toolbot.py` (below) added to the dev config as the `dummy-tools`
model. Run it in four terminals:

```bash
# terminal 1
uv run dummy
# terminal 2
uv run python scripts/anthropic_toolbot.py
# terminal 3
CONFIG_YAML=./dev.config.yaml uv run lumen
# terminal 4
LUMEN_API_KEY=sk_... uv run --with claude-agent-sdk python scripts/anthropic_smoke.py
```

Override defaults with `BASE_URL` (default `http://localhost:5001`), `MODEL` (default
`dummy`) and `TOOL_MODEL` (default `dummy-tools`). Checks that need a client that is
not installed are skipped, not failed. It exits non-zero if any check fails.

### anthropic_toolbot.py

A tiny OpenAI-compatible backend on `:9998` that makes one tool call: the first time a
request carries tools and no tool result, it calls the first tool it recognises
(`Bash` or `bash`), and once the result comes back it answers with text quoting it. The echo
backend (`uv run dummy`) never calls tools, so this is what exercises the
tool_use/tool_result round trip. Add it to the dev config as `dummy-tools`:

```yaml
- name: dummy-tools
  input_cost_per_million: 0
  output_cost_per_million: 0
  endpoints:
  - url: http://localhost:9998/v1
    api_key: dummy
    model: dummy-tools
```

Besides the smoke script, `tests/routes/test_api_anthropic.py::test_claude_code_over_real_http`
imports it as its live upstream, so a change to its behaviour affects that test when
`claude` is installed.
