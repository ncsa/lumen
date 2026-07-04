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

For polished docs, point the config at the `illinois` theme, set `app.name: Lumen`,
and configure a real model (e.g. set `MODEL=<name>`) so the chat shows real answers.
Override defaults with `BASE_URL`, `OUTPUT_DIR`, `MODEL`, and `CHROME_PATH`.

The Usage screenshot is not generated here: usage analytics require PostgreSQL
(TimescaleDB), so capture `docs/img/usage.png` from a populated Postgres instance.
