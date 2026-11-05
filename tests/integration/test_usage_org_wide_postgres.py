"""The org-wide /usage arms on the hierarchical views, proven equal on real TimescaleDB.

After #177 the org-wide charts read ``request_counts_hourly`` (week, month) and
``request_counts_daily`` (year, all), both built over
``request_counts_hourly_by_entity``; since #429 the heatmap reads
``request_counts_hourly_total`` (every period), built over the hourly view. The point of that change is an
invariant, and this module proves it per period and per endpoint:

1. Org-wide equals raw ``request_logs`` with the same buckets.
2. Org-wide equals the sum of the per-entity API results plus the raw rows of a
   hard-deleted entity. The per-entity API hides deleted entities, the org-wide
   totals keep their traffic (their raw rows have a NULL ``entity_id``). Since
   ``c8d0e2f4a6b8`` the foreign key is ``RESTRICT`` and no delete can leave such
   rows, but history written under ``SET NULL`` keeps them, so the seed writes
   them directly.

The seed has several entities, models and sources, costs with six decimal
places, rows across more than a year, rows at UTC midnight and on each window's
first instant, and rows in the current hour. It is materialised by ``flask
upgrade``, the step that runs before the app starts.

Every assertion runs twice (the ``phase`` fixture): once on the views as
``flask upgrade`` leaves them, and once after fresh rows land above every
watermark, where only the real-time chain through the views sees them.

The background jobs of this module's private database are paused, so no policy
moves a watermark under a test.
"""

import os
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus

import pytest
from sqlalchemy import create_engine, event, select, text

from lumen.commands import USAGE_AGGREGATES, upgrade_cmd
from tests.conftest import no_snapshot_refresher

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

PERIODS = ["week", "month", "year", "all"]
BUCKETS = {"week": "1 day", "month": "1 day", "year": "1 week", "all": "1 month"}
ENDPOINTS = ["summary", "requests", "tokens", "models", "heatmap"]
SOURCES = ("chat", "api")
DELETED = "deleted"

# The view each org-wide arm reads, per period.
EXPECTED_VIEW = {
    (endpoint, period): (
        "request_counts_hourly_total" if endpoint == "heatmap"
        else "request_counts_hourly" if period in ("week", "month")
        else "request_counts_daily"
    )
    for endpoint in ENDPOINTS for period in PERIODS
}


# --------------------------------------------------------------------------
# Raw oracles: the same buckets the endpoints use, over request_logs.
# --------------------------------------------------------------------------

RAW = {
    "summary": """
        SELECT COUNT(*), COALESCE(SUM(input_tokens + output_tokens), 0), COALESCE(SUM(cost), 0)
        FROM request_logs rl WHERE true {clause}
    """,
    "requests": """
        SELECT time_bucket(CAST(:bucket AS INTERVAL), time), COUNT(*)
        FROM request_logs rl WHERE true {clause} GROUP BY 1 ORDER BY 1
    """,
    "tokens": """
        SELECT time_bucket(CAST(:bucket AS INTERVAL), time), SUM(input_tokens + output_tokens)
        FROM request_logs rl WHERE true {clause} GROUP BY 1 ORDER BY 1
    """,
    "models": """
        SELECT mc.model_name, COUNT(*)
        FROM request_logs rl JOIN model_configs mc ON rl.model_config_id = mc.id
        WHERE true {clause} GROUP BY 1
    """,
    "heatmap": """
        SELECT EXTRACT(DOW FROM time), EXTRACT(HOUR FROM time), COUNT(*)
        FROM request_logs rl WHERE true {clause} GROUP BY 1, 2
    """,
}


def _raw(engine, endpoint, period, start, deleted_only=False):
    clause, params = "", {}
    if start is not None:
        clause += " AND rl.time >= :start"
        params["start"] = start
    if deleted_only:
        clause += " AND rl.entity_id IS NULL"
    if endpoint in ("requests", "tokens"):
        params["bucket"] = BUCKETS[period]
    with engine.connect() as conn:
        rows = conn.execute(text(RAW[endpoint].format(clause=clause)), params).all()
    if endpoint == "summary":
        count, tokens, cost = rows[0]
        return {"requests": int(count), "tokens": int(tokens), "cost": Decimal(cost)}
    if endpoint in ("requests", "tokens"):
        return [{"period": r[0].isoformat(), "count": int(r[1])} for r in rows]
    if endpoint == "models":
        return Counter({r[0]: int(r[1]) for r in rows})
    return Counter({(int(r[0]), int(r[1])): int(r[2]) for r in rows})


def _normalise(endpoint, body):
    """An endpoint's JSON as something that sums: a Counter, or the summary's totals."""
    if endpoint == "summary":
        return {"requests": body["requests"], "tokens": body["tokens"],
                "cost": Decimal(str(round(body["cost"], 6)))}
    if endpoint in ("requests", "tokens"):
        return Counter({r["period"]: r["count"] for r in body})
    if endpoint == "models":
        return Counter({r["model"]: r["requests"] for r in body})
    return Counter({(c["dow"], c["hour"]): c["count"] for c in body})


def _add(endpoint, a, b):
    if endpoint == "summary":
        return {k: a[k] + b[k] for k in a}
    return a + b


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_migrated_isolated):
    return pg_migrated_isolated[1]


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    url = pg_migrated_isolated[0]
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    # config.Config read DATABASE_URL when the root conftest imported it; without
    # this the app stays on SQLite, where every /usage endpoint answers empty.
    from config import Config
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app
    with pytest.MonkeyPatch.context() as mp:
        no_snapshot_refresher(mp)
        application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        from lumen.extensions import db
        assert db.engine.dialect.name == "postgresql"

    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(
            "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
            "WHERE job_id >= 1000"
        ))
    admin.dispose()
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


def _period_start(period):
    from lumen.blueprints.profile.routes import _usage_period_start
    return _usage_period_start(period)


def _insert(conn, rng, when, eid, model, source):
    conn.execute(text(
        "INSERT INTO request_logs (time, entity_id, model_config_id, source, input_tokens, "
        "output_tokens, cost, duration, outcome, aborted) "
        "VALUES (:t, :e, :m, :s, :i, :o, :c, 0.5, 'ok', false)"
    ), {"t": when, "e": eid, "m": model, "s": source,
        "i": rng.randint(1, 5000), "o": rng.randint(1, 5000),
        # Six decimal places, the column's scale, so any float detour shows.
        "c": Decimal(rng.randint(1, 9_999_999)) / Decimal(1_000_000)})


def _seed_times(now):
    """Instants spread over more than a year, with the edges a bucket can get wrong."""
    times = []
    for days in (420, 380, 366, 300, 200, 150, 95, 61, 45, 32, 29, 20, 12, 8, 6, 3, 1):
        times.append(now - timedelta(days=days, hours=days % 24, minutes=days % 60))
    # UTC midnight exactly, and the instant before it: a day and a week edge.
    for days in (2, 9, 40, 200):
        midnight = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        times += [midnight, midnight - timedelta(microseconds=1)]
    # Each window's first instant, and the instant before it.
    for period in ("week", "month", "year"):
        start = _period_start(period)
        times += [start, start - timedelta(microseconds=1)]
    return times


@pytest.fixture(scope="module")
def world(pg_app, engine):
    """Seed, with a hard-deleted entity's NULL-entity rows, then run ``flask upgrade``."""
    rng = random.Random(242)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        models = [conn.execute(text(
            "INSERT INTO model_configs (model_name, input_cost_per_million, output_cost_per_million) "
            "VALUES (:n, 1, 2) RETURNING id"
        ), {"n": f"orgq-model-{i}"}).scalar() for i in range(3)]
        entities = {key: conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', :n, 'OQ', true) RETURNING id"
        ), {"n": f"orgq-{key}"}).scalar() for key in ("a", "b", "c")}
        # What an entity deleted under the old ON DELETE SET NULL left behind.
        entities[DELETED] = None
        for when in _seed_times(now):
            for key, eid in entities.items():
                for source in SOURCES:
                    if rng.random() < 0.7:
                        _insert(conn, rng, when, eid, rng.choice(models), source)
        # The current hour, before the upgrade: it stays above every watermark.
        for eid in entities.values():
            _insert(conn, rng, now, eid, models[0], "chat")
        assert conn.execute(text(
            "SELECT COUNT(*) FROM request_logs WHERE entity_id IS NULL"
        )).scalar() > 0, "the seed wrote no NULL-entity rows"

    with pg_app.app_context():
        result = pg_app.test_cli_runner().invoke(upgrade_cmd, [])
    assert result.exit_code == 0, result.output

    with pg_app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        admin = db.session.execute(
            select(Entity).filter_by(email="admin@example.com")
        ).scalar_one_or_none()
        if admin is None:
            admin = Entity(entity_type="user", email="admin@example.com", name="Admin",
                           initials="AD", active=True)
            db.session.add(admin)
            db.session.commit()
        admin_id = admin.id

    live = [eid for key, eid in entities.items() if key != DELETED]
    return {"models": models, "live": live, "admin": admin_id, "rng": rng}


def _watermark(conn, view):
    return conn.execute(text(
        "SELECT _timescaledb_functions.to_timestamp(_timescaledb_functions.cagg_watermark(mat_hypertable_id)) "
        "FROM _timescaledb_catalog.continuous_agg WHERE user_view_name = :v"
    ), {"v": view}).scalar()


@pytest.fixture(scope="module", params=["materialised", "fresh"])
def phase(request, world, engine):
    """``materialised``: as ``flask upgrade`` left it. ``fresh``: plus rows above every watermark."""
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    open_bucket = {"request_counts_hourly_by_entity": hour, "request_counts_hourly": hour,
                   "request_counts_hourly_total": hour, "request_counts_daily": hour.replace(hour=0),
                   "request_counts_daily_by_entity": hour.replace(hour=0)}
    with engine.connect() as conn:
        marks = {view: _watermark(conn, view) for view in USAGE_AGGREGATES}
        # The upgrade materialised every closed bucket that holds rows; a watermark
        # ends after the newest of them, so only the open bucket is left real-time.
        for view in USAGE_AGGREGATES:
            newest_closed = conn.execute(text(
                "SELECT MAX(time) FROM request_logs WHERE time < :b"
            ), {"b": open_bucket[view]}).scalar()
            assert marks[view] > newest_closed, (view, marks[view], newest_closed)

    if request.param == "fresh":
        rng = world["rng"]
        with engine.begin() as conn:
            for eid in world["live"]:
                for model in world["models"]:
                    _insert(conn, rng, datetime.now(timezone.utc), eid, model, "api")
            _insert(conn, rng, datetime.now(timezone.utc), None, world["models"][1], "chat")
            newest = conn.execute(text("SELECT MAX(time) FROM request_logs")).scalar()
        assert all(newest >= mark for mark in marks.values()), (newest, marks)
    return request.param


def _client(pg_app, admin_id):
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = admin_id
        sess["admin_mode"] = True
    return client


def _get(client, endpoint, period, eid=None):
    path = f"/api/usage/{endpoint}?period={period}"
    if eid is not None:
        path += f"&entity_id={eid}"
    resp = client.get(path)
    assert resp.status_code == HTTPStatus.OK, resp.data
    return resp.get_json()


# --------------------------------------------------------------------------
# 1 + 2. Equality, per period and endpoint, in both phases.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("period", PERIODS)
def test_org_wide_equals_raw_and_the_per_entity_sum(pg_app, engine, world, phase, period, endpoint):
    client = _client(pg_app, world["admin"])
    with pg_app.app_context():
        start = _period_start(period)
    body = _get(client, endpoint, period)
    org = _normalise(endpoint, body)

    # 1. Org-wide against raw request_logs with the same buckets.
    raw = _raw(engine, endpoint, period, start)
    if endpoint in ("requests", "tokens"):
        assert body == raw, "the org-wide series differs from raw"
        raw = _normalise(endpoint, raw)
    assert org == raw
    if endpoint == "summary":
        assert body["requests"] > 0
    else:
        assert org, "nothing to compare"

    # 2. Org-wide against the per-entity API plus the deleted entity's raw rows.
    deleted = _raw(engine, endpoint, period, start, deleted_only=True)
    if endpoint in ("requests", "tokens"):
        deleted = _normalise(endpoint, deleted)
    assert (deleted["requests"] if endpoint == "summary" else sum(deleted.values())) > 0
    total = deleted
    for eid in world["live"]:
        total = _add(endpoint, total, _normalise(endpoint, _get(client, endpoint, period, eid)))
    assert org == total


def _grid(cells):
    """A heatmap as the full 7x24 grid, zeros included: ``grid[dow][hour]``."""
    return [[cells.get((dow, hour), 0) for hour in range(24)] for dow in range(7)]


@pytest.mark.parametrize("period", PERIODS)
def test_org_wide_heatmap_is_the_raw_seven_by_twenty_four_grid(pg_app, engine, world, phase, period):
    """The heatmap off ``request_counts_hourly_total`` cell for cell against raw request_logs."""
    with pg_app.app_context():
        start = _period_start(period)
    body = _get(_client(pg_app, world["admin"]), "heatmap", period)
    grid = _grid(_normalise("heatmap", body))
    assert grid == _grid(_raw(engine, "heatmap", period, start))
    assert sum(1 for row in grid for cell in row if cell) > 1, "the seed fills a single cell"


# --------------------------------------------------------------------------
# 3. Which view each arm reads, and what the planner touches for "all".
# --------------------------------------------------------------------------

def _captured(pg_app, client, endpoint, period):
    """The statements an org-wide request runs, with their parameters."""
    seen = []
    with pg_app.app_context():
        from lumen.extensions import db
        target = db.engine

    def listener(conn, cursor, statement, parameters, context, executemany):
        seen.append((statement, parameters))

    event.listen(target, "before_cursor_execute", listener)
    try:
        _get(client, endpoint, period)
    finally:
        event.remove(target, "before_cursor_execute", listener)
    return [s for s in seen if "request_counts_" in s[0]]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("period", PERIODS)
def test_each_org_wide_arm_reads_the_planned_view(pg_app, world, period, endpoint):
    (statement, _), = _captured(pg_app, _client(pg_app, world["admin"]), endpoint, period)
    expected = EXPECTED_VIEW[(endpoint, period)]
    # The trailing space or newline keeps request_counts_hourly from matching
    # request_counts_hourly_total.
    views = {v for v in ("request_counts_hourly_by_entity", "request_counts_hourly",
                         "request_counts_hourly_total", "request_counts_daily") if f"FROM {v} " in statement or f"FROM {v}\n" in statement}
    assert views == {expected}, statement


def _relations(plan):
    found = set()
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "Relation Name" in node:
                found.add(node["Relation Name"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def test_all_summary_plan_reads_the_daily_materialisation_and_no_old_raw_chunk(pg_app, engine, world):
    (statement, params), = _captured(pg_app, _client(pg_app, world["admin"]), "summary", "all")
    with engine.connect() as conn:
        plan = conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, params or {}).scalar()
        daily_id = conn.execute(text(
            "SELECT mat_hypertable_id FROM _timescaledb_catalog.continuous_agg "
            "WHERE user_view_name = 'request_counts_daily'"
        )).scalar()
        daily_chunks = set(conn.execute(text(
            "SELECT c.table_name FROM _timescaledb_catalog.chunk c WHERE c.hypertable_id = :h"
        ), {"h": daily_id}).scalars())
        mark = _watermark(conn, "request_counts_hourly_by_entity")
        old_raw = set(conn.execute(text(
            "SELECT chunk_name FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND range_end <= :m"
        ), {"m": mark}).scalars())
        all_raw = set(conn.execute(text(
            "SELECT chunk_name FROM timescaledb_information.chunks WHERE hypertable_name = 'request_logs'"
        )).scalars())

    relations = _relations(plan)
    assert daily_chunks and relations & daily_chunks, \
        f"the plan reads none of request_counts_daily's materialised chunks: {sorted(relations)}"
    assert old_raw, "the seed left no request_logs chunk below the watermark; nothing to prove"
    assert not relations & old_raw, \
        f"the plan scans request_logs chunks below the watermark: {sorted(relations & old_raw)}"
    # The real-time tail still reaches raw rows, only the newest chunk(s).
    assert relations & all_raw <= all_raw - old_raw
