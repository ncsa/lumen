"""Timings for the dashboard endpoints on every range, over a full retention window.

Seeds 31d of 1-minute probes and samples (the default `refresh.web` and
`retention`) and a year of daily rows per model and source (which `1y` reads,
and which are kept forever), then times `/api/overview`, `/api/lumen` and `/api/series` for
each range on SQLite, and on PostgreSQL when `MONITOR_TEST_POSTGRES_URL` is set.
The default test run skips it; run it with

    uv run pytest tests/bench_ranges.py -m bench -s

`MONITOR_BENCH_MODELS` sets the model count (default 13, as in production) and
`MONITOR_BENCH_REPEAT` the requests per endpoint and range (default 3; the table
shows the median and the slowest).
"""

import os
import random
import statistics
import time
from datetime import datetime, timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import delete, insert, text

from monitor import migrate, web
from monitor.buckets import RANGES
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory, utcnow
from monitor.models import DailyStat, Probe, Sample
from monitor.probe import LiveSnapshot, Snapshot

# Seeding alone can outlast the suite-wide timeout.
pytestmark = [pytest.mark.bench, pytest.mark.timeout(0)]

MODELS = int(os.environ.get("MONITOR_BENCH_MODELS", "13"))
REPEAT = int(os.environ.get("MONITOR_BENCH_REPEAT", "3"))
SPAN = timedelta(days=31)
DAYS = 400  # More than the 1y range covers.
STEP = timedelta(minutes=1)
CHUNK = 50_000
CONFIG = "lumen: {url: 'https://lumen.test'}\n"


def model_ids(count: int) -> list[str]:
    return [f"org/model-{i:02d}" for i in range(count)]


def _status(rnd: random.Random) -> str:
    roll = rnd.random()
    return "down" if roll < 0.01 else "degraded" if roll < 0.03 else "ok"


def _insert(session, table, rows: list[dict]) -> None:
    for i in range(0, len(rows), CHUNK):
        session.execute(insert(table), rows[i:i + CHUNK])


def daily_rows(rnd: random.Random, now: datetime, models: list[str]) -> list[dict]:
    """A lumen, hardware and models.dev row per model for each of the last `DAYS` UTC days (the config's timezone).
    Past days are final and fully counted; today is counted up to `now`."""
    today = now.date()
    rows = []
    for back in range(DAYS, -1, -1):
        day = today - timedelta(days=back)
        start = datetime(day.year, day.month, day.day)
        covered = min(start + timedelta(days=1), now)
        polls = int((covered - start) / STEP)
        for model in models:
            polls_ok = polls - rnd.randint(0, polls // 20)
            shared = {"day": day, "model": model, "last_updated": covered, "final": back > 0}
            rows.append({
                **shared, "source": "lumen", "input_price": 0.2, "output_price": 0.8,
                "requests": rnd.uniform(0, 30) * polls, "input_tokens": rnd.uniform(0, 25_000) * polls,
                "output_tokens": rnd.uniform(0, 10_000) * polls, "covered_until": covered,
                "input_tokens_covered_until": covered, "output_tokens_covered_until": covered,
                "configured": 2, "healthy": 2, "status": "ok", "polls": polls, "polls_ok": polls_ok,
                "healthy_sum": polls_ok * 2,
            })
            rows.append({
                **shared, "source": "hardware", "daily_cost": rnd.uniform(50, 500), "gpu": "H100", "gpus": 2,
                "gpu_price": 2000.0, "energy": 700.0, "kwh": 0.1, "avg_healthy": rnd.uniform(1, 2),
                "polls": polls, "polls_ok": polls_ok, "healthy_sum": polls_ok * 2,
            })
            rows.append({
                **shared, "source": "models.dev", "modelsdev_id": model, "input_median": 0.3, "input_p5": 0.1,
                "input_p95": 0.9, "output_median": 1.2, "output_p5": 0.4, "output_p95": 3.0, "providers": 8,
            })
    return rows


def seed(session_factory, now, models: list[str]) -> None:
    """One probe per minute over `SPAN`, a sample per model for each successful probe, and `daily_rows`."""
    rnd = random.Random(344)
    probes, samples = [], []
    at = now - SPAN
    while at <= now:
        success = rnd.random() > 0.002
        probes.append({
            "polled_at": at, "success": success, "response_time_ms": rnd.uniform(20, 400) if success else None,
            "model_count": len(models) if success else None, "models_up": len(models) if success else None,
        })
        if success:
            for model in models:
                tokens = rnd.random() > 0.1
                samples.append({
                    "model": model, "sampled_at": at, "status": _status(rnd), "configured": 2, "healthy": rnd.choice((1, 2)),
                    "requests_last_minute": rnd.randint(0, 60),
                    "input_tokens_last_minute": rnd.randint(0, 50_000) if tokens else None,
                    "output_tokens_last_minute": rnd.randint(0, 20_000) if tokens else None,
                })
        at += STEP
    with session_factory() as session:
        for table in (Sample, Probe, DailyStat):
            session.execute(delete(table))
        _insert(session, Probe, probes)
        _insert(session, Sample, samples)
        _insert(session, DailyStat, daily_rows(rnd, now, models))
        session.commit()
        if session.get_bind().dialect.name == "postgresql":
            # Production tables are analysed by autovacuum; fresh ones are not.
            session.execute(text("ANALYZE"))
            session.commit()


def snapshot(now, models: list[str]) -> LiveSnapshot:
    live = LiveSnapshot()
    live.set(Snapshot(now, [
        {"id": m, "description": m, "status": "ok", "configured": 2, "healthy": 2, "input_price": 0.2, "output_price": 0.8,
         "requests": {"last_minute": 3, "last_hour": 100, "last_day": 2000, "last_month": None},
         "tokens": {"last_minute": 3000, "last_hour": 100000, "last_day": 2000000, "last_month": None}}
        for m in models
    ]))
    return live


def run(url: str, tmp_path, label: str) -> None:
    models = model_ids(MODELS)
    migrate.upgrade(url)
    engine = make_engine(url)
    try:
        session_factory = make_session_factory(engine)
        now = utcnow().replace(second=0, microsecond=0)
        started = time.perf_counter()
        seed(session_factory, now, models)
        seeded = time.perf_counter() - started
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG)
        client = web.create_app(ConfigStore(path, env={}), session_factory, snapshot(now, models)).test_client()
        endpoints = {
            "/api/overview": {},
            "/api/lumen": {},
            "/api/series": {"model": models[0]},
        }
        lines = [
            "",
            f"{label}: {MODELS} models, 31d of 1m probes, {DAYS}d of daily rows, seeded in {seeded:.1f}s; median / max of {REPEAT} requests, ms",
            f"{'range':<6}" + "".join(f"{endpoint:>24}" for endpoint in endpoints),
        ]
        for key in RANGES:
            cells = []
            for endpoint, params in endpoints.items():
                timings = []
                for _ in range(REPEAT):
                    started = time.perf_counter()
                    response = client.get(endpoint, query_string={**params, "range": key})
                    timings.append(1000 * (time.perf_counter() - started))
                    assert response.status_code == HTTPStatus.OK, (endpoint, key, response.status_code)
                cells.append(f"{statistics.median(timings):.0f} / {max(timings):.0f}")
            lines.append(f"{key:<6}" + "".join(f"{cell:>24}" for cell in cells))
        print("\n".join(lines))
    finally:
        engine.dispose()


def test_bench_sqlite(tmp_path, capsys):
    with capsys.disabled():
        run(f"sqlite:///{tmp_path / 'monitor.db'}", tmp_path, "SQLite")


@pytest.mark.postgres
def test_bench_postgres(postgres_url, tmp_path, capsys):
    # Drops every table on exit: point MONITOR_TEST_POSTGRES_URL at a throwaway database only.
    try:
        with capsys.disabled():
            run(postgres_url, tmp_path, "PostgreSQL")
    finally:
        migrate.downgrade(postgres_url, "base")
