# Lumen monitor

A standalone service that watches a Lumen instance. It probes `GET /v1/models`, records
health samples, daily request and token totals, and the daily models.dev, Lumen and hardware
prices in its own database (SQLite or PostgreSQL), and serves a dashboard.

It imports nothing from the main `lumen` package and has its own dependencies, migrations,
tests and Docker image.

> Work in progress: this step adds the config, the schema, the migrations and CI. The
> collectors, the scheduler (`monitor serve`) and the dashboard follow.

## Setup

```bash
cd scripts/monitor
cp config.yaml.example config.yaml   # then edit
uv sync
uv run monitor migrate               # create or upgrade the schema
uv run monitor collect --price       # record today's Lumen, models.dev and hardware prices once
```

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
