# Dev scripts

Helper scripts for local testing and for regenerating the help-doc screenshots.
Both drive a real browser with Playwright, so install the browser once:

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
export LUMEN_SECRET_KEY=dev LUMEN_ENCRYPTION_KEY=dev
export DATABASE_URL=postgresql://USER:PASS@HOST:5432/lumen_shots
export CONFIG_YAML=./scripts/screenshot.config.yaml MODEL=qwen2.5-7b-instruct

uv run flask --app "lumen:create_app" db upgrade   # build schema on a fresh DB
uv run python scripts/screenshot_seed.py           # seed usage + a conversation
uv run dummy &                                     # echo backend on :9999
uv run python -c "from lumen import create_app; create_app().run(port=5002)" &
BASE_URL=http://localhost:5002 uv run python scripts/screenshots.py
```
