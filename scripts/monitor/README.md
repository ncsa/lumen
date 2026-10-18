# Lumen monitor

A standalone service that watches a Lumen instance. It probes `GET /v1/models`, records
health samples, daily request and token totals, and the daily models.dev, Lumen and hardware
prices in its own database (SQLite or PostgreSQL), and serves a dashboard.

It imports nothing from the main `lumen` package and has its own dependencies, migrations,
tests and Docker image.

> Work in progress: the config, schema, migrations, CI, the price collectors, the health
> probe, the daily request/token totals and the scheduler (`monitor serve`) are in. The
> dashboard follows.

## Setup

```bash
cd scripts/monitor
cp config.yaml.example config.yaml   # then edit
uv sync
uv run monitor migrate               # create or upgrade the schema
uv run monitor collect --probe       # probe GET /v1/models once: health, response time, samples
uv run monitor collect --price       # record today's Lumen, models.dev and hardware prices once
uv run monitor serve                 # run the probe, models and price loops until SIGTERM/SIGINT
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
There is no HTTP server yet: port 5000 in the image is for the dashboard, which follows.

The models run makes no HTTP call: it reads the request and token windows from the last
successful probe (skipping the run when that probe is older than 2 × `refresh.web`) and adds
the traffic since `covered_until` to today's lumen row. A gap up to 1h is counted from
`last_hour`; up to 24h from `last_day`, marked `estimated`; a longer one from `last_month`
(at most 30 days back), filling the missed days with `estimated` rows, or without `last_month`
from the last 24h only, leaving the missed days empty. A gap across local midnight is split
between the days by duration. A snapshot without any usable window leaves `covered_until`
where it was, so its gap is counted by the next snapshot that has one. `covered_until` is stored, so a restart never counts the same
time twice. The first run after local midnight marks the earlier days `final`; a day's totals
are exact only when it is `final` and not `estimated`.

`--config PATH` (or `MONITOR_CONFIG`) picks another config file. These environment variables
override the file: `MONITOR_DATABASE_URL`, `MONITOR_BASE_PATH`, `MONITOR_LUMEN_API_KEY`.
`config.yaml.example` documents every key.

## Docker

```bash
docker build -t lumen-monitor scripts/monitor
docker run -v $PWD/config.yaml:/app/config.yaml lumen-monitor
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
