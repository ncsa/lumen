# Lumen monitor

A standalone service that watches a Lumen instance. It probes `GET /v1/models`, records
health samples, daily request and token totals, and the daily models.dev, Lumen and hardware
prices in its own database (SQLite or PostgreSQL), and serves a dashboard.

It imports nothing from the main `lumen` package and has its own dependencies, migrations,
tests and Docker image.

> Work in progress: the config, schema, migrations, CI, the price collectors, the health
> probe, the daily request/token totals, the scheduler and the overview dashboard are in. The
> model detail page follows.

## Setup

```bash
cd scripts/monitor
cp config.yaml.example config.yaml   # then edit
uv sync
uv run monitor migrate               # create or upgrade the schema
uv run monitor collect --probe       # probe GET /v1/models once: health, response time, samples
uv run monitor collect --price       # record today's Lumen, models.dev and hardware prices once
uv run monitor serve                 # the loops plus the dashboard on :5000 until SIGTERM/SIGINT
```

A probe writes one `probes` row (success, response time, models listed and up) and, when it
succeeds, one `samples` row per model plus today's availability counters. The Lumen state
comes from the last 3 probes: DOWN when all failed, DEGRADED when the latest one lists a model
that is down, UP otherwise, UNKNOWN before the first probe. The first probe of each local day
deletes probes and samples older than `retention`.

`monitor serve` runs three loops on one scheduler thread: the probe every `refresh.web`, the
models run every `refresh.models` and the price run every `refresh.price` (which also re-reads
`config.yaml`). A loop that fails is logged and tried again on its next turn without
affecting the others. SIGTERM or SIGINT lets the running loop finish, then the service exits.

The models run makes no HTTP call: it reads the request and token windows from the last
successful probe (skipping the run when that probe is older than 2 × `refresh.web`) and adds
the traffic since each metric's watermark (`covered_until` for requests, `tokens_covered_until`
for tokens) to today's lumen row. A gap up to 1h is counted from `last_hour`; up to 24h from
`last_day`, marked `estimated`; a longer one from `last_month` (at most 30 days back), filling
the missed days with `estimated` rows, or without `last_month` from the last 24h only, leaving
the missed days empty. A gap across local midnight is split between the days by duration. A
snapshot without a usable window for a metric leaves that metric's watermark where it was, so
its gap is counted by the next snapshot that has one. The watermarks are stored, so a restart
never counts the same time twice.

Earlier days are marked `final` once every metric is counted past their end, normally on the
first run after local midnight. A day whose traffic is still deferred stays open until a window
covers it, or until it is 30 days old and no window can reach it; it then closes as
`estimated`, since part of its traffic was never counted. A metric that stops being reported
for good keeps every later day of that model open for those 30 days, not only the day of the
gap. A day's totals are exact
only when it is `final` and not `estimated`.

## Dashboard

![Overview](docs/overview.png)

`monitor serve` also serves the dashboard (no login) on port 5000 (`--host`, `--port`), in
the same process as the loops so it sees the last probe. Every page, API and static file sits
under `base_path` (restart after changing it). The overview has:

- the Lumen card: UP / DEGRADED / DOWN, last / min / avg / max response time, uptime (a
  degraded Lumen counts as up) and a response-time strip;
- status cards: active models, healthy / configured instances, requests and tokens in the last
  hour and today (tokens show "—" until Lumen reports them);
- the model table (canonical models only): a status strip, uptime (the share of buckets that
  are ok), instances, our price, the models.dev median (p5–p95), hardware $/day and $/M tokens
  (or $/request without token counts), peak req/min and Lumen's 24h requests and tokens.
  Columns sort on click; a row opens the model's detail page.

Ranges are 1h, 12h, 24h, 7d and 30d from the raw probes and samples, bucketed at 1m, 3m, 5m,
30m and 2h (never below `refresh.web`); a bucket shows its worst status and is "no data" when
empty. 1y uses one bucket per local day: ok at 99% availability or more, degraded above 0%,
down otherwise. `*` marks values that include days which are not yet `final` or are
`estimated`; `†` marks a model without a `models:` GPU entry. The page reloads its data every
`refresh.page` without a full reload. The JSON behind it is `/api/overview?range=&sort=&dir=`
and `/api/lumen?range=`.

`--config PATH` (or `MONITOR_CONFIG`) picks another config file. These environment variables
override the file: `MONITOR_DATABASE_URL`, `MONITOR_BASE_PATH`, `MONITOR_LUMEN_API_KEY`.
`config.yaml.example` documents every key.

## Docker

```bash
docker build -t lumen-monitor scripts/monitor
docker run -p 5000:5000 -v $PWD/config.yaml:/app/config.yaml lumen-monitor
```

The container runs `monitor migrate && monitor serve`.

## Tests

```bash
uv run pytest
```

PostgreSQL tests are marked `postgres` and skip unless `MONITOR_TEST_POSTGRES_URL` points at
a throwaway database:

```bash
docker run -d --rm --name monitor-test-pg -e POSTGRES_USER=monitor -e POSTGRES_PASSWORD=monitor -e POSTGRES_DB=monitor_ci -p 55434:5432 postgres:17
MONITOR_TEST_POSTGRES_URL=postgresql://monitor:monitor@localhost:55434/monitor_ci uv run pytest -rs
docker stop monitor-test-pg
```
