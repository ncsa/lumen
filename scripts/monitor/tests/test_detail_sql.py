"""The SQL detail page (`series`, `_bucket_stats`) against the Python path it replaces.

Each test runs on SQLite, and on PostgreSQL when `MONITOR_TEST_POSTGRES_URL` is set.
"""

import dataclasses
import random
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select

from monitor import migrate
from monitor.buckets import RANGES, Grid, bucket_statuses, make_grid, model_uptime, range_days
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.detail import (
    _bucket_price_points,
    _hardware_points,
    _mean,
    _price_points,
    _rows_by_source,
    find_model,
    is_daily,
    series,
)
from monitor.models import DailyStat, Sample, token_sum
from monitor.overview import _iso, exact, model_rows

NOW = datetime(2026, 10, 17, 17, 0, 12, 345678)  # off the second, like utcnow()
MICROSECOND = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)
MODEL = "org/qwen"
OTHER = "org/other"
# starting is unknown to STATUS_PRIORITY; rare failures, so a bucket of many samples is not always down
STATUSES, WEIGHTS = ["ok", "degraded", "down", "starting"], [95, 2, 1, 2]


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def session_factory(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'monitor.db'}"
    else:
        url = request.getfixturevalue("postgres_url")
    migrate.upgrade(url)
    engine = make_engine(url)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()
        if request.param == "postgresql":
            migrate.downgrade(url, "base")


@pytest.fixture
def make_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('lumen: {url: "https://lumen.test"}\ntimezone: America/Chicago\n')
    base = ConfigStore(path, env={}).current

    def make(web=timedelta(minutes=1)):
        return dataclasses.replace(base, timezone=ZoneInfo("America/Chicago"),
                                   refresh=dataclasses.replace(base.refresh, web=web))

    return make


def add(session_factory, rows):
    with session_factory() as session:
        session.add_all(rows)
        session.commit()


def old_series(session, config, snapshot, model_id, rng, now):
    """`series` as it was before the SQL bucketing: the samples read twice, once for the header's strip
    and peak and once for the bucket means, and each time every sample into Python."""
    model = find_model(session, snapshot, model_id)
    if model is None:
        return None
    tz = config.timezone
    days = range_days(rng, now, tz)
    grid = make_grid(rng, config.refresh.web, now, tz)
    times = [_iso(t) for t in grid.times()]
    stats = None
    if not rng.daily:
        samples = session.execute(
            select(Sample.sampled_at, Sample.status, Sample.healthy, Sample.configured, Sample.requests_last_minute,
                   Sample.input_tokens_last_minute, Sample.output_tokens_last_minute)
            .where(Sample.model == model_id, Sample.sampled_at > now - rng.span, Sample.sampled_at <= now)
        ).all()
        strip = bucket_statuses(((s.sampled_at, s.status) for s in samples), grid)
        rpms = [s.requests_last_minute for s in samples if s.requests_last_minute is not None]
        stats = {model_id: {"strip": strip, "uptime": model_uptime(strip), "peak_rpm": max(rpms) if rpms else None}}
        buckets = [[] for _ in range(grid.count)]
        for s in samples:
            i = grid.index(s.sampled_at)
            if i is not None:
                buckets[i].append(s)
    (header,) = model_rows(session, config, rng, now, [model], stats)
    rows = _rows_by_source(session, model_id, days)
    hardware, hardware_range = _hardware_points(rows, days, tz)

    if rng.daily:
        lumen = [rows.get(("lumen", d)) for d in days]
        instances = {"unit": "day", "points": [
            {"t": d.isoformat(), "healthy": r.healthy_sum / r.polls if r and r.polls else None, "configured": r.configured if r else None}
            for d, r in zip(days, lumen)
        ]}
    else:
        instances = {"unit": "bucket", "points": [
            {"t": t, "healthy": _mean(s.healthy for s in b), "configured": _mean(s.configured for s in b)}
            for t, b in zip(times, buckets)
        ]}

    if is_daily(rng):
        points = []
        for day in days:
            r = rows.get(("lumen", day))
            points.append({
                "t": day.isoformat(),
                "requests": r.requests if r else None,
                "tokens": r.tokens if r else None,
                "input_tokens": r.input_tokens if r else None,
                "output_tokens": r.output_tokens if r else None,
                "estimated": r is not None and not exact(r),
            })
        traffic = {"unit": "day", "points": points}
    else:
        traffic = {"unit": "minute", "points": [
            {"t": t, "requests": _mean(s.requests_last_minute for s in b),
             "tokens": _mean(token_sum(s.input_tokens_last_minute, s.output_tokens_last_minute) for s in b),
             "input_tokens": _mean(s.input_tokens_last_minute for s in b),
             "output_tokens": _mean(s.output_tokens_last_minute for s in b), "estimated": False}
            for t, b in zip(times, buckets)
        ]}
    traffic["average"] = {
        key: _mean(p[key] for p in traffic["points"]) for key in ("requests", "tokens", "input_tokens", "output_tokens")
    }

    return {
        "range": rng.key,
        "model": model_id,
        "generated_at": _iso(now),
        "daily": is_daily(rng),
        "header": header,
        "prices": _price_points(rows, days) if is_daily(rng) else _bucket_price_points(session, model_id, rows, days, grid, tz),
        "traffic": traffic,
        "hardware": {"points": hardware, "range": hardware_range},
        "instances": instances,
    }


def both(session_factory, config, rng, model_id=MODEL, now=NOW):
    """`(new, old)` series for the model."""
    with session_factory() as session:
        new = series(session, config, None, model_id, rng, now)
        old = old_series(session, config, None, model_id, rng, now)
    return new, old


def maybe(rnd, value, p=0.15):
    return None if rnd.random() < p else value


def sample(at, rnd, model=MODEL):
    """A sample at `at` with a random status and random, sometimes unknown, counts.

    Token counts reach a billion per minute, so input + output and the bucket sums pass 2³¹.
    """
    return Sample(
        model=model, sampled_at=at, status=rnd.choices(STATUSES, WEIGHTS)[0],
        configured=maybe(rnd, rnd.randint(1, 4)), healthy=maybe(rnd, rnd.randint(0, 4)),
        requests_last_minute=maybe(rnd, rnd.randint(0, 500)),
        input_tokens_last_minute=maybe(rnd, rnd.randint(0, 1_500_000_000)),
        output_tokens_last_minute=maybe(rnd, rnd.randint(0, 1_000_000_000)),
    )


def edges(grid: Grid):
    """Every bucket edge, and one microsecond either side of it."""
    for k in range(grid.count + 1):
        edge = grid.start + k * grid.size
        yield from (edge - MICROSECOND, edge, edge + MICROSECOND)


def random_samples(rnd, grid, start, now):
    """Random samples over `(start, now]`, on a third of the bucket edges, one at `now`, some just outside
    the range, and samples of another model."""
    span = (now - start) // MICROSECOND
    times = [start + rnd.randrange(1, span + 1) * MICROSECOND for _ in range(600)]
    times += [t for t in edges(grid) if start < t <= now and rnd.random() < 0.3]
    times += [now, start, start - MICROSECOND, now + MICROSECOND]
    return [sample(t, rnd) for t in times] + [sample(t, rnd, OTHER) for t in times[:50]]


@pytest.mark.parametrize("key", ["1h", "12h", "24h", "7d", "30d"])
@pytest.mark.parametrize("web", [timedelta(minutes=1), timedelta(minutes=7, seconds=13)])
def test_short_ranges_match_the_python_path(session_factory, make_config, key, web):
    config, rng = make_config(web), RANGES[key]
    grid = make_grid(rng, web, NOW, config.timezone)
    add(session_factory, random_samples(random.Random(f"{key}-{web}"), grid, NOW - rng.span, NOW))
    new, old = both(session_factory, config, rng)
    assert new == old
    assert new["instances"]["unit"] == "bucket" and any(p["healthy"] is not None for p in new["instances"]["points"])
    assert len(set(new["header"]["strip"]) - {"unknown"}) >= 2


def test_bucket_means_skip_unknown_values(session_factory, make_config):
    """A NULL leaves its column's mean alone, and the token mean skips a sample missing either side."""
    config, rng = make_config(), RANGES["1h"]
    grid = make_grid(rng, config.refresh.web, NOW, config.timezone)
    at = grid.start + 10 * grid.size  # on the edge, so in bucket 10
    add(session_factory, [
        Sample(model=MODEL, sampled_at=at, status="ok", configured=2, healthy=None, requests_last_minute=4,
               input_tokens_last_minute=30, output_tokens_last_minute=None),
        Sample(model=MODEL, sampled_at=at + SECOND, status="down", configured=None, healthy=1, requests_last_minute=None,
               input_tokens_last_minute=20, output_tokens_last_minute=5),
        Sample(model=MODEL, sampled_at=at + 2 * SECOND, status="ok", configured=3, healthy=None,
               requests_last_minute=7, input_tokens_last_minute=None, output_tokens_last_minute=None),
    ])
    new, old = both(session_factory, config, rng)
    assert new == old
    assert new["instances"]["points"][10] == {"t": _iso(at), "healthy": 1.0, "configured": 2.5}
    traffic = new["traffic"]["points"][10]
    assert (traffic["requests"], traffic["tokens"], traffic["input_tokens"], traffic["output_tokens"]) == (5.5, 25.0, 25.0, 5.0)
    assert new["header"]["strip"][10] == "down" and new["header"]["peak_rpm"] == 7


@pytest.mark.parametrize("key", ["1h", "7d", "1y"])
def test_a_model_with_only_daily_rows_matches_the_python_path(session_factory, make_config, key):
    add(session_factory, [DailyStat(day=date(2026, 10, 16), model=MODEL, source="lumen", requests=50.0, polls=10,
                                    polls_ok=9, healthy_sum=10, final=True)])
    new, old = both(session_factory, make_config(), RANGES[key])
    assert new == old
    assert new["header"]["peak_rpm"] is None or key == "1y"


# --- Reads ---------------------------------------------------------------------------


def sample_reads(session_factory, config, rng):
    """The series, and every statement (with its parameters) that read `samples` while it was built."""
    reads = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if re.search(r"\bFROM samples\b", statement):
            reads.append((statement, parameters))

    with session_factory() as session:
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            result = series(session, config, None, MODEL, rng, NOW)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    return result, reads


@pytest.mark.parametrize("key", ["24h", "30d"])
def test_the_database_groups_the_samples(session_factory, make_config, key):
    """One grouped read gets the bucket means and the header; no statement returns the samples one row each.

    The only other reads of `samples` are `current_models`' latest probe (no live snapshot here).
    """
    config, rng = make_config(), RANGES[key]
    grid = make_grid(rng, config.refresh.web, NOW, config.timezone)
    rnd = random.Random(key)
    starts = [grid.start + k * grid.size + SECOND for k in (10, 100, 200)]
    add(session_factory, [sample(start + i * SECOND, rnd) for start in starts for i in range(100)])
    result, reads = sample_reads(session_factory, config, rng)
    assert result["header"]["strip"][10] != "unknown"

    grouped = [r for r in reads if "GROUP BY" in r[0]]
    assert len(grouped) == 1, reads
    with session_factory() as session:
        replayed = [session.connection().exec_driver_sql(statement, parameters).all() for statement, parameters in reads]
    assert len(replayed[reads.index(grouped[0])]) == len(starts)  # 300 samples come back as 3 rows
    assert all(len(rows) <= len(starts) for rows in replayed), [len(rows) for rows in replayed]
