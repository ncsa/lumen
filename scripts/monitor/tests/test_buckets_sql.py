"""The SQL bucketing (`bucket_index`, `status_rank`) against the Python path it replaces.

Each test runs on SQLite, and on PostgreSQL when `MONITOR_TEST_POSTGRES_URL` is set.
"""

import itertools
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from monitor import migrate
from monitor.buckets import (
    RANGES,
    STATUS_PRIORITY,
    UNKNOWN,
    Grid,
    bucket_index,
    bucket_statuses,
    make_grid,
    model_uptime,
    rank_status,
    status_rank,
    worst,
)
from monitor.db import make_engine, make_session_factory
from monitor.models import Sample
from monitor.overview import _short_range_stats

NOW = datetime(2026, 10, 17, 17, 0, 12, 345678)  # off the second, like utcnow()
MICROSECOND = timedelta(microseconds=1)
STATUSES = ["ok", "degraded", "down", "starting", "stopping"]  # the last two are unknown to STATUS_PRIORITY
MODELS = ["alpha", "beta", "gamma"]


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


def add(session_factory, rows):
    with session_factory() as session:
        session.add_all(rows)
        session.commit()


def old_short_range_stats(session, grid, start, now, models):
    """`_short_range_stats` as it was before the SQL bucketing: every sample into Python."""
    stmt = (
        select(Sample.model, Sample.sampled_at, Sample.status, Sample.requests_last_minute)
        .where(Sample.sampled_at > start, Sample.sampled_at <= now, Sample.model.in_(models))
    )
    points = {m: [] for m in models}
    peaks = {m: None for m in models}
    for s in session.execute(stmt).all():
        points[s.model].append((s.sampled_at, s.status))
        if s.requests_last_minute is not None:
            peaks[s.model] = max(peaks[s.model] or 0, s.requests_last_minute)
    stats = {}
    for m in models:
        strip = bucket_statuses(points[m], grid)
        stats[m] = {"strip": strip, "uptime": model_uptime(strip), "peak_rpm": peaks[m]}
    return stats


def edges(grid: Grid):
    """Every bucket edge, and one microsecond either side of it."""
    for k in range(grid.count + 1):
        edge = grid.start + k * grid.size
        yield from (edge - MICROSECOND, edge, edge + MICROSECOND)


def sql_indexes(session_factory, grid, times):
    add(session_factory, [Sample(model="alpha", sampled_at=t, status="ok") for t in times])
    with session_factory() as session:
        stmt = select(Sample.sampled_at, bucket_index(Sample.sampled_at, grid)).where(Sample.sampled_at >= grid.start)
        return dict(session.execute(stmt).all())


# --- bucket_index ----------------------------------------------------------------


@pytest.mark.parametrize("key", ["1h", "24h", "30d"])
def test_bucket_index_matches_grid_index_on_every_edge(session_factory, key):
    grid = make_grid(RANGES[key], timedelta(minutes=1), NOW, None)
    times = [t for t in edges(grid) if t >= grid.start]
    found = sql_indexes(session_factory, grid, times)
    assert len(found) == len(times)
    for t in times:
        expected = grid.index(t)
        assert (found[t] if 0 <= found[t] < grid.count else None) == expected, t


def test_bucket_index_matches_grid_index_on_random_times(session_factory):
    rnd = random.Random(184)
    grid = Grid(datetime(2026, 9, 17, 3, 7, 41, 999999), timedelta(minutes=7, microseconds=3), 7000)
    span = grid.count * grid.size // MICROSECOND
    times = {grid.start + rnd.randrange(span) * MICROSECOND for _ in range(2000)}
    found = sql_indexes(session_factory, grid, times)
    assert {t: found[t] for t in times} == {t: grid.index(t) for t in times}


def test_julianday_misses_the_bucket_edge_that_bucket_index_gets(session_factory):
    """Why `bucket_index` counts whole microseconds: SQLite's `julianday()` keeps
    milliseconds only, and as a float, so a sample just before an edge lands in
    the next bucket. This pins that edge case."""
    grid = Grid(datetime(2026, 10, 17, 15, 0, 0, 123456), timedelta(minutes=5), 24)
    late = grid.start + grid.size - MICROSECOND  # the last microsecond of bucket 0
    assert grid.index(late) == 0
    assert sql_indexes(session_factory, grid, [late])[late] == 0
    with session_factory() as session:
        if session.get_bind().dialect.name != "sqlite":
            return
        floats = session.execute(text(
            "SELECT CAST((julianday(sampled_at) - julianday(:start)) * 86400 / :size AS REAL) FROM samples"
        ), {"start": grid.start.isoformat(" "), "size": grid.size.total_seconds()}).scalar_one()
    assert floats >= 1  # the float path puts it in bucket 1


# --- status_rank ----------------------------------------------------------------


def test_status_rank_maps_back_to_worst(session_factory):
    groups = [list(g) for n in range(4) for g in itertools.combinations_with_replacement(STATUSES, n)]
    t0 = datetime(2026, 10, 17)
    add(session_factory, [
        Sample(model=f"m{i:03d}", sampled_at=t0 + j * timedelta(seconds=1), status=s)
        for i, g in enumerate(groups) for j, s in enumerate(g)
    ])
    with session_factory() as session:
        ranks = dict(session.execute(select(Sample.model, func.max(status_rank(Sample.status))).group_by(Sample.model)).all())
    for i, g in enumerate(groups):
        assert rank_status(ranks.get(f"m{i:03d}")) == worst(g), g
    assert rank_status(None) == UNKNOWN
    assert {rank_status(p) for p in STATUS_PRIORITY.values()} == set(STATUS_PRIORITY)


# --- _short_range_stats -----------------------------------------------------------


def random_samples(rnd, grid, start, now):
    """Random samples over the range plus some on the edges, some unknown statuses, some NULL req/min,
    one at `now`, and some outside the range or of a model not asked for."""
    span = (now - start) // MICROSECOND
    times = [start + rnd.randrange(1, span + 1) * MICROSECOND for _ in range(400)]
    times += [t for t in edges(grid) if start < t <= now and rnd.random() < 0.3]
    times += [now, start, start - MICROSECOND, now + MICROSECOND]
    rows = []
    for t in times:
        model = rnd.choice(MODELS + ["other"])
        rpm = None if rnd.random() < 0.2 else rnd.randrange(0, 50)
        rows.append(Sample(model=model, sampled_at=t, status=rnd.choice(STATUSES), requests_last_minute=rpm))
    return rows


@pytest.mark.parametrize("key", ["1h", "12h", "24h", "7d", "30d"])
@pytest.mark.parametrize("web", [timedelta(minutes=1), timedelta(minutes=7, seconds=13)])
def test_short_range_stats_match_the_python_path(session_factory, key, web):
    rng = RANGES[key]
    grid = make_grid(rng, web, NOW, None)
    start = NOW - rng.span
    rnd = random.Random(f"{key}-{web}")
    add(session_factory, random_samples(rnd, grid, start, NOW))
    models = MODELS + ["silent"]  # a model without samples
    with session_factory() as session:
        new = _short_range_stats(session, grid, start, NOW, models)
        old = old_short_range_stats(session, grid, start, NOW, models)
    assert new == old
    assert new["silent"] == {"strip": [UNKNOWN] * grid.count, "uptime": None, "peak_rpm": None}
