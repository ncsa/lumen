"""The Phase 8 query rewrite: /usage's five per-entity endpoints, on real Timescale.

Contract item (a) of the plan is blunt about why this file exists — creating
``request_counts_hourly_by_entity`` protects nothing on its own. Until the five
``if eid:`` branches in ``lumen/blueprints/profile/routes.py`` actually read it,
enabling retention truncates every individual's "All Time" history while the
org-wide charts, fed by ``request_counts_hourly``, keep going. So what is proven
here is equality, not existence:

1. Each rewritten endpoint returns exactly what the raw ``request_logs`` query
   it replaced returns, over the same window, for every period it offers.

2. **Every one of those comparisons includes a row inside the current hour
   bucket.** Seeded with history alone they pass at 0 == 0 while certifying the
   exact regression the phase exists to prevent: an aggregate that has not
   materialised the current hour, or is ``materialized_only``, answers zero for
   the student who ran forty requests in the 9 a.m. lab and opened /usage at
   09:50. The ``entity_usage`` fixture materialises history first and writes the
   current-bucket rows only afterwards, so they are provably above the
   watermark and reachable only through real-time aggregation.

3. The heatmap sees 24 distinct hours. On a daily bucket ``EXTRACT(HOUR FROM
   bucket)`` collapses every row to hour 0 and the 7x24 grid becomes a single
   column, with no error anywhere — contract item (b).

4. Retention-drop simulation: chunks are dropped out from under the raw table
   and the per-user charts still render, from the aggregate, while
   ``entity_stats`` lifetime totals are untouched.

5. The fallback fires. The aggregate is created ``WITH NO DATA`` by a migration
   that ``entrypoint.sh`` runs at container start, while the backfill is a
   manual command; history the policy never materialised is *invisible* through
   the view, not merely stale. A window starting before the aggregate's
   earliest bucket must therefore be answered from raw.

6. The SQLite early return still short-circuits before any of this SQL runs.

7. The window's first hour is only partial — ``start`` is ``now - offset``, which
   is mid-hour in general, while ``bucket`` is the hour's start. Section 7 seeds
   a row inside ``[start, next hour)`` for week/month/year, which the fixture
   above structurally cannot: its rows are at fixed whole-day and whole-hour
   depths.

8. A hard-deleted entity answers the same through either arm. The foreign key
   nulls the raw rows; the aggregate materialised the id and never re-checks it.

This module runs against ``pg_migrated_isolated`` — a database of its own — for
the reasons that fixture's docstring gives.
"""

import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest
from sqlalchemy import exc, text

from .conftest import TEST_CONFIG

SOURCE = "ph8q"  # request_logs.source is VARCHAR(8)
VIEW = "request_counts_hourly_by_entity"

# Depths, in days, kept far apart on purpose: each test owns one and they must
# not share a 7-day chunk, because the retention test drops one by range.
MAIN_OLD = 200
HEAT_DAY = 10
RETENTION_OLD = 300
FALLBACK_OLD = 500

# The five endpoints and the periods each is asserted over. 'week' is the page
# default; 'all' is the one retention would have silently emptied.
PERIODS = ["week", "month", "year", "all"]


# --------------------------------------------------------------------------
# The raw queries these endpoints used to run, verbatim, as the oracle. If the
# rewrite drifts from them, an equality test fails rather than a reviewer
# having to notice.
# --------------------------------------------------------------------------

RAW_SUMMARY = """
    SELECT COALESCE(COUNT(*), 0),
           COALESCE(SUM(input_tokens + output_tokens), 0),
           COALESCE(SUM(cost), 0.0)
    FROM request_logs
    WHERE entity_id = :eid {clause}
"""

RAW_REQUESTS = """
    SELECT time_bucket(CAST(:bucket AS INTERVAL), time) AS period, COUNT(*) AS count
    FROM request_logs
    WHERE entity_id = :eid {clause}
    GROUP BY 1 ORDER BY 1
"""

RAW_TOKENS = """
    SELECT time_bucket(CAST(:bucket AS INTERVAL), time) AS period,
           SUM(input_tokens + output_tokens) AS tokens
    FROM request_logs
    WHERE entity_id = :eid {clause}
    GROUP BY 1 ORDER BY 1
"""

RAW_MODELS = """
    SELECT mc.model_name, COUNT(*) AS requests
    FROM request_logs rl
    JOIN model_configs mc ON rl.model_config_id = mc.id
    WHERE rl.entity_id = :eid {clause}
    GROUP BY mc.model_name ORDER BY requests DESC
"""

RAW_HEATMAP = """
    SELECT EXTRACT(DOW FROM time) AS dow, EXTRACT(HOUR FROM time) AS hour, COUNT(*) AS count
    FROM request_logs
    WHERE entity_id = :eid {clause}
    GROUP BY 1, 2
"""

_INSERT = f"""
    INSERT INTO request_logs
        (time, entity_id, model_config_id, source, input_tokens, output_tokens,
         cost, duration, ttft_visible, outcome, aborted)
    VALUES (:time, :eid, :model, '{SOURCE}', :inp, :out, :cost, :dur, :ttft, 'ok', false)
"""


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _refresh(engine, view=VIEW, deadline=90.0, window=None):
    """Materialise the view's full history, retrying while a policy job holds it.

    The aggregates carry background refresh policies and TimescaleDB refuses an
    overlapping refresh outright rather than waiting. Nothing about that is
    specific to tests — an operator backfill hits the same wall — so retrying is
    the whole remedy.

    ``window`` narrows the refresh to one ``(start, end)`` pair. Tests that add
    history *after* the module fixture's full refresh need it: those rows sit
    below the watermark, so they are invisible until materialised, and a second
    unbounded refresh would materialise the current hour too — destroying the
    one thing ``test_the_current_hour_is_included_in_every_endpoint`` proves.
    """
    import time as _time
    if window is None:
        sql, params = f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)", {}
    else:
        sql = f"CALL refresh_continuous_aggregate('{view}', :w_start, :w_end)"
        params = {"w_start": window[0], "w_end": window[1]}
    end = _time.monotonic() + deadline
    while True:
        try:
            with _autocommit(engine) as conn:
                conn.execute(text(sql), params)
            return
        except exc.OperationalError as err:
            if "concurrent refresh" not in str(err) or _time.monotonic() > end:
                raise
            _time.sleep(1)


def _insert(conn, *, eid, model, when, inp=10, out=20, cost="0.25", dur=1.5, ttft=0.75):
    conn.execute(text(_INSERT.replace(":time", when)), {
        "eid": eid, "model": model, "inp": inp, "out": out,
        "cost": cost, "dur": dur, "ttft": ttft,
    })


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_migrated(pg_migrated_isolated):
    return pg_migrated_isolated[1]


@pytest.fixture(scope="module")
def pg_url(pg_migrated_isolated):
    return pg_migrated_isolated[0]


@pytest.fixture(scope="module")
def pg_app(pg_migrated, pg_url):
    """A real Flask app bound to the migrated PostgreSQL database.

    The root ``conftest`` app is SQLite and every /usage endpoint returns an
    empty payload there, so it cannot exercise a line of this SQL. ``create_app``
    reads ``DATABASE_URL`` (see ``config.py``), which is restored afterwards so
    the session-scoped SQLite app the rest of the suite shares is unaffected.
    """
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    # ``config.Config`` reads DATABASE_URL at class-definition time, and the
    # root conftest's SQLite app has already imported it, so setting the
    # environment variable alone silently leaves this app on SQLite — which
    # every /usage endpoint answers with an empty payload rather than an error.
    from config import Config
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = pg_url
    from lumen import create_app
    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        from lumen.extensions import db
        assert db.engine.dialect.name == "postgresql", (
            f"the test app is on {db.engine.dialect.name}; every assertion below "
            "would pass against the SQLite early return without touching Timescale"
        )
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


@pytest.fixture(scope="module")
def entity_usage(pg_app, pg_migrated):
    """Seed history, materialise it, then seed the current bucket.

    The order is the point, and it is the order production is in. History goes
    in first and is materialised with an explicit full refresh — what the
    operator backfill does — which advances the watermark to the end of the last
    complete bucket. The current-hour rows are written *after* that, so they sit
    above the watermark and are visible only through real-time aggregation. Any
    assertion that counts them is therefore a direct test of
    ``materialized_only = false``; under the 2.27 default they are simply gone.

    Returns the ids the tests need, plus the timestamp the current-bucket rows
    carry so a test can name that bucket without re-reading the clock and racing
    an hour boundary.
    """
    with pg_migrated.begin() as conn:
        models = [
            conn.execute(text(
                "INSERT INTO model_configs (model_name, input_cost_per_million, output_cost_per_million) "
                "VALUES (:n, 1, 2) RETURNING id"
            ), {"n": f"ph8q-model-{i}"}).scalar()
            for i in (1, 2)
        ]
        ents = {}
        for key in ("main", "heat", "ret", "fb"):
            ents[key] = conn.execute(text(
                "INSERT INTO entities (entity_type, name, initials, active) "
                "VALUES ('user', :n, 'PQ', true) RETURNING id"
            ), {"n": f"ph8q-{key}"}).scalar()

        # --- history for the equality tests: one row per period boundary it
        # must straddle, on two different models so usage_models has a ranking.
        for when, model, inp, out in [
            (f"now() - INTERVAL '{MAIN_OLD} days'", models[0], 11, 21),
            ("now() - INTERVAL '25 days'", models[1], 12, 22),
            ("now() - INTERVAL '3 days'", models[0], 13, 23),
            ("now() - INTERVAL '3 days'", models[0], 14, 24),
            ("now() - INTERVAL '2 hours'", models[1], 15, 25),
        ]:
            _insert(conn, eid=ents["main"], model=model, when=when, inp=inp, out=out)

        # --- one row in each of the 24 hours of a single past day, for the
        # heatmap. A daily bucket would collapse all of them onto hour 0.
        for hour in range(24):
            _insert(
                conn, eid=ents["heat"], model=models[0],
                when=(f"date_trunc('day', now() - INTERVAL '{HEAT_DAY} days') "
                      f"+ INTERVAL '{hour} hours'"),
            )

        # --- retention-drop simulation: a chunk of its own, far from every
        # other depth used here, so it can be dropped by range.
        for _ in range(3):
            _insert(conn, eid=ents["ret"], model=models[0],
                    when=f"now() - INTERVAL '{RETENTION_OLD} days'", inp=100, out=200)

        conn.execute(text(
            "INSERT INTO entity_stats (entity_id, requests, input_tokens, output_tokens, "
            "audio_seconds, cost, conversations) VALUES (:e, 4, 400, 800, 0, 1.0, 0)"
        ), {"e": ents["ret"]})

    _refresh(pg_migrated)

    # --- and only now the current hour, above the watermark.
    with pg_migrated.begin() as conn:
        now = conn.execute(text(
            _INSERT.replace(":time", "now()") + " RETURNING time"
        ), {"eid": ents["main"], "model": models[0], "inp": 16, "out": 26,
            "cost": "0.25", "dur": 1.5, "ttft": 0.75}).scalar()
        _insert(conn, eid=ents["main"], model=models[1], when="now()", inp=17, out=27)
        _insert(conn, eid=ents["heat"], model=models[0], when="now()")
        _insert(conn, eid=ents["ret"], model=models[0], when="now()", inp=100, out=200)

    yield {"entities": ents, "models": models, "now": now}

    # No teardown: pg_migrated_isolated drops the whole database.


def _client(pg_app, eid):
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = eid
    return client


@pytest.fixture(scope="module")
def main_client(pg_app, entity_usage):
    return _client(pg_app, entity_usage["entities"]["main"])


def _period_start(pg_app, period):
    with pg_app.app_context():
        from lumen.blueprints.profile.routes import _usage_period_start
        return _usage_period_start(period)


def _covers(pg_app, start):
    with pg_app.test_request_context():
        from lumen.blueprints.profile.routes import _entity_aggregate_covers
        return _entity_aggregate_covers(start)


def _raw(engine, sql, period_start, eid, bucket=None, alias=""):
    clause = ""
    params = {"eid": eid}
    if period_start is not None:
        clause = f"AND {alias}time >= :start"
        params["start"] = period_start
    if bucket is not None:
        params["bucket"] = bucket
    with engine.connect() as conn:
        return conn.execute(text(sql.format(clause=clause)), params).all()


def _json(client, path, period):
    resp = client.get(f"{path}?period={period}")
    assert resp.status_code == HTTPStatus.OK, resp.data
    return resp.get_json()


def _bucket_for(period):
    return {"week": "1 day", "month": "1 day", "year": "1 week", "all": "1 month"}[period]


def _assert_models_match(body, expected):
    """Compare the two arms' model rankings without depending on tie order.

    ``ORDER BY requests DESC`` is not a total order: two models on the same
    count may come back in either order, and the two arms sort different row
    sets (hourly groups vs individual requests), so they are free to disagree
    on which tied model comes first. Comparing sorted pairs keeps every
    model-and-count assertion intact, and the second assertion keeps the
    ordering contract the endpoint actually promises.
    """
    assert (sorted((m["model"], m["requests"]) for m in body)
            == sorted((r[0], int(r[1])) for r in expected))
    counts = [m["requests"] for m in body]
    assert counts == sorted(counts, reverse=True), "the rows are not in requests-DESC order"


# --------------------------------------------------------------------------
# 1 + 2. Equality, per endpoint, per period, current bucket included.
# --------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.parametrize("period", PERIODS)
def test_summary_matches_the_raw_query(pg_app, pg_migrated, main_client, entity_usage, period):
    eid = entity_usage["entities"]["main"]
    start = _period_start(pg_app, period)
    assert _covers(pg_app, start) is True, (
        "the aggregate does not cover this window, so the endpoint fell back to "
        "raw and this test would compare raw against raw"
    )
    expected = _raw(pg_migrated, RAW_SUMMARY, start, eid)[0]
    body = _json(main_client, "/api/usage/summary", period)

    assert body["requests"] == int(expected[0])
    assert body["tokens"] == int(expected[1])
    assert body["cost"] == pytest.approx(float(expected[2]))
    assert body["requests"] > 0


@pytest.mark.postgres
@pytest.mark.parametrize("period", PERIODS)
def test_requests_chart_matches_the_raw_query(pg_app, pg_migrated, main_client, entity_usage, period):
    eid = entity_usage["entities"]["main"]
    start = _period_start(pg_app, period)
    assert _covers(pg_app, start) is True
    expected = [
        {"period": r[0].isoformat(), "count": int(r[1])}
        for r in _raw(pg_migrated, RAW_REQUESTS, start, eid, bucket=_bucket_for(period))
    ]
    assert _json(main_client, "/api/usage/requests", period) == expected
    assert expected


@pytest.mark.postgres
@pytest.mark.parametrize("period", PERIODS)
def test_tokens_chart_matches_the_raw_query(pg_app, pg_migrated, main_client, entity_usage, period):
    eid = entity_usage["entities"]["main"]
    start = _period_start(pg_app, period)
    assert _covers(pg_app, start) is True
    expected = [
        {"period": r[0].isoformat(), "count": int(r[1])}
        for r in _raw(pg_migrated, RAW_TOKENS, start, eid, bucket=_bucket_for(period))
    ]
    assert _json(main_client, "/api/usage/tokens", period) == expected
    assert expected


@pytest.mark.postgres
@pytest.mark.parametrize("period", PERIODS)
def test_models_chart_matches_the_raw_query(pg_app, pg_migrated, main_client, entity_usage, period):
    eid = entity_usage["entities"]["main"]
    start = _period_start(pg_app, period)
    assert _covers(pg_app, start) is True
    expected = _raw(pg_migrated, RAW_MODELS, start, eid, alias="rl.")
    _assert_models_match(_json(main_client, "/api/usage/models", period), expected)
    assert expected


@pytest.mark.postgres
@pytest.mark.parametrize("period", PERIODS)
def test_heatmap_matches_the_raw_query(pg_app, pg_migrated, main_client, entity_usage, period):
    eid = entity_usage["entities"]["main"]
    start = _period_start(pg_app, period)
    assert _covers(pg_app, start) is True
    expected = sorted(
        (int(r[0]), int(r[1]), int(r[2]))
        for r in _raw(pg_migrated, RAW_HEATMAP, start, eid)
    )
    body = _json(main_client, "/api/usage/heatmap", period)
    assert sorted((c["dow"], c["hour"], c["count"]) for c in body) == expected
    assert expected


@pytest.mark.postgres
def test_the_current_hour_is_included_in_every_endpoint(pg_app, pg_migrated, main_client, entity_usage):
    """The single most load-bearing assertion in this file.

    The current-hour rows were written after the fixture's refresh, so they are
    above the aggregate's watermark and can only be reached through real-time
    aggregation. Under ``materialized_only = true`` — the TimescaleDB 2.27
    default — every number below drops by exactly those two rows, silently.
    """
    now = entity_usage["now"]
    eid = entity_usage["entities"]["main"]
    with pg_migrated.connect() as conn:
        in_bucket = conn.execute(text(
            f"SELECT SUM(requests) FROM {VIEW} WHERE entity_id = :e "
            "AND bucket = date_trunc('hour', CAST(:t AS timestamptz))"
        ), {"e": eid, "t": now}).scalar()
        raw_total = conn.execute(text(
            "SELECT COUNT(*) FROM request_logs WHERE entity_id = :e"
        ), {"e": eid}).scalar()

    assert in_bucket == 2, (
        "the current hour bucket is missing from the aggregate — this is what "
        "materialized_only = true looks like, and it is what a student sees at "
        "09:50 after a 9 a.m. lab"
    )
    assert _json(main_client, "/api/usage/summary", "all")["requests"] == raw_total
    # Two rows 3 days back, one 2 hours back, and the two current-hour rows.
    assert sum(p["count"] for p in _json(main_client, "/api/usage/requests", "week")) == 5


# --------------------------------------------------------------------------
# 3. The heatmap keeps its 24 columns.
# --------------------------------------------------------------------------

@pytest.mark.postgres
def test_heatmap_returns_twenty_four_distinct_hours_from_the_aggregate(pg_app, entity_usage):
    """Contract (b): a daily bucket collapses every row onto hour 0.

    There would be no error — just a 7x24 grid rendered as a single column. The
    seed puts one request in each of the 24 hours of one day, so an hourly
    bucket is the only bucket width that can produce this result.
    """
    client = _client(pg_app, entity_usage["entities"]["heat"])
    start = _period_start(pg_app, "month")
    assert _covers(pg_app, start) is True
    body = _json(client, "/api/usage/heatmap", "month")

    assert sorted({cell["hour"] for cell in body}) == list(range(24))
    assert sum(cell["count"] for cell in body) == 25, "the current-hour row is missing"


# --------------------------------------------------------------------------
# 4. Retention-drop simulation.
# --------------------------------------------------------------------------

@pytest.mark.postgres
def test_charts_survive_dropped_chunks_and_lifetime_totals_are_unchanged(
    pg_app, pg_migrated, entity_usage
):
    """Drop the raw chunk and read the same numbers back out of the aggregate.

    This is the failure the whole phase exists to prevent, run forwards: before
    the rewrite these endpoints scanned ``request_logs``, so dropping a chunk
    erased that history from every per-user chart while the org-wide charts,
    fed by ``request_counts_hourly``, carried on. ``entity_stats`` is asserted
    alongside because it is the cumulative record the UI promises survives
    retention.
    """
    eid = entity_usage["entities"]["ret"]
    client = _client(pg_app, eid)

    before_summary = _json(client, "/api/usage/summary", "all")
    before_requests = _json(client, "/api/usage/requests", "all")
    with pg_migrated.connect() as conn:
        before_stats = conn.execute(text(
            "SELECT requests, input_tokens, output_tokens, cost FROM entity_stats WHERE entity_id = :e"
        ), {"e": eid}).one()
    assert before_summary["requests"] == 4

    with _autocommit(pg_migrated) as conn:
        dropped = conn.execute(text(
            "SELECT drop_chunks('request_logs', "
            f"older_than => now() - INTERVAL '{RETENTION_OLD - 10} days', "
            f"newer_than => now() - INTERVAL '{RETENTION_OLD + 10} days')"
        )).scalars().all()
    assert dropped, "no chunk was dropped, so this test proves nothing about retention"

    with pg_migrated.connect() as conn:
        surviving_raw = conn.execute(text(
            "SELECT COUNT(*) FROM request_logs WHERE entity_id = :e"
        ), {"e": eid}).scalar()
        after_stats = conn.execute(text(
            "SELECT requests, input_tokens, output_tokens, cost FROM entity_stats WHERE entity_id = :e"
        ), {"e": eid}).one()

    assert surviving_raw == 1, "the raw history is supposed to be gone by now"
    assert _json(client, "/api/usage/summary", "all") == before_summary
    assert _json(client, "/api/usage/requests", "all") == before_requests
    assert tuple(after_stats) == tuple(before_stats), "retention touched the lifetime totals"


# --------------------------------------------------------------------------
# 5. The fallback to raw.
# --------------------------------------------------------------------------

@pytest.fixture
def unbackfilled_history(pg_migrated, entity_usage):
    """History older than anything the aggregate holds, deliberately never refreshed.

    This is the state a deploy lands in. ``entrypoint.sh`` runs ``flask db
    upgrade`` at container start, so the aggregate and its policy exist
    immediately, while ``flask backfill-aggregate`` is a separate manual step.
    The policy's ``start_offset`` is 30 days; once it runs, the watermark moves
    past everything older, and real-time aggregation only scans raw rows *above*
    the watermark — so rows below it that were never materialised are invisible
    through the view, not merely stale.
    """
    eid = entity_usage["entities"]["fb"]
    with pg_migrated.begin() as conn:
        for _ in range(2):
            _insert(conn, eid=eid, model=entity_usage["models"][0],
                    when=f"now() - INTERVAL '{FALLBACK_OLD} days'", inp=5, out=7)
        _insert(conn, eid=eid, model=entity_usage["models"][0], when="now()", inp=5, out=7)
    yield eid
    with pg_migrated.begin() as conn:
        conn.execute(text("DELETE FROM request_logs WHERE entity_id = :e"), {"e": eid})


@pytest.mark.postgres
def test_fallback_to_raw_when_the_aggregate_does_not_reach_back(
    pg_app, pg_migrated, unbackfilled_history
):
    eid = unbackfilled_history
    with pg_migrated.connect() as conn:
        through_view = conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {VIEW} WHERE entity_id = :e"
        ), {"e": eid}).scalar()
    assert int(through_view) == 1, (
        "the un-materialised rows are visible through the view, so this test is "
        "not exercising the un-backfilled state it claims to"
    )
    assert _covers(pg_app, None) is False

    client = _client(pg_app, eid)
    body = _json(client, "/api/usage/summary", "all")
    assert body["requests"] == 3, "the endpoint answered from the aggregate and lost history"
    assert body["tokens"] == 36
    assert sum(p["count"] for p in _json(client, "/api/usage/requests", "all")) == 3
    assert sum(c["count"] for c in _json(client, "/api/usage/heatmap", "all")) == 3
    assert sum(m["requests"] for m in _json(client, "/api/usage/models", "all")) == 3
    assert sum(p["count"] for p in _json(client, "/api/usage/tokens", "all")) == 36


@pytest.mark.postgres
def test_a_covered_window_still_uses_the_aggregate_while_older_history_is_unbackfilled(
    pg_app, unbackfilled_history
):
    """The fallback is per-window, not a global kill switch.

    'Week' is inside what the aggregate holds even when a year of history is
    not, and answering that from raw would give up the whole point of the
    rewrite for the period the page opens on.
    """
    assert _covers(pg_app, _period_start(pg_app, "week")) is True
    assert _covers(pg_app, None) is False


# --------------------------------------------------------------------------
# 6. The SQLite early return.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,empty", [
    ("/api/usage/summary", {"requests": 0, "tokens": 0, "cost": 0.0, "new_users": 0, "last_active": None}),
    ("/api/usage/requests", []),
    ("/api/usage/tokens", []),
    ("/api/usage/models", []),
    ("/api/usage/heatmap", []),
])
def test_sqlite_short_circuits_before_any_timescale_sql(auth_client, path, empty):
    """Deliberately not a ``postgres`` test — SQLite is the whole point.

    Dev and the rest of the suite run on SQLite, where neither
    ``request_counts_hourly_by_entity`` nor ``time_bucket`` exists. Delete the
    ``if db.engine.dialect.name != "postgresql":`` guard from any of these five
    endpoints and it raises ``OperationalError: no such table`` instead, so this
    parametrisation fails five times over.
    """
    resp = auth_client.get(f"{path}?period=all")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == empty


def test_sqlite_usage_page_still_renders(auth_client):
    assert auth_client.get("/usage").status_code == HTTPStatus.OK


# --------------------------------------------------------------------------
# 7. The window's first, partial hour.
# --------------------------------------------------------------------------
#
# ``bucket`` is ``time_bucket('1 hour', time)`` — the hour's *start*. So
# ``bucket >= :start`` and ``time >= :start`` are the same predicate only when
# ``start`` is hour-aligned. On a mid-hour start every row in
# ``[start, next hour)`` is counted by the raw arm and dropped by the aggregate
# arm, and the whole ``entity_usage`` seed misses it: its rows sit at
# ``now() - {200d, 25d, 3d, 2h}`` and ``now()``, none of which can ever land in
# the first partial hour of a 7 / 30 / 365-day window. Every equality assertion
# above therefore passes with that skew live.
#
# The second-order effect is the reason this is not merely cosmetic. On deploy
# the aggregate is ``WITH NO DATA``, ``_entity_aggregate_covers`` is False and
# every window is answered from raw — correctly. The first time the refresh
# policy runs, ``covers`` flips True and the same numbers *drop* by whatever
# happened in that first hour, with nothing to tell the user why.

BOUNDARY_PERIODS = {"week": 7, "month": 30, "year": 365}

# Minutes past the hour for the frozen clock and for the probe rows. The clock
# sits mid-hour — that is the entire point — and each probe sits later in the
# same hour, so it is inside the window for ``time >= :start`` while carrying
# ``bucket`` = that hour's start, which is *before* an unfloored ``:start``.
_CLOCK_MINUTE = 17
_PROBE_MINUTE = 30


@pytest.fixture(scope="module")
def boundary_usage(pg_app, pg_migrated, entity_usage):
    """One row inside the first, partial hour of each of week / month / year.

    Its own entity, so none of the counts asserted above move. The rows are
    written after ``entity_usage`` has already refreshed the whole view, which
    puts them below the watermark and therefore invisible until materialised —
    so each is materialised by a refresh bounded to exactly its own hour. A
    second unbounded refresh would drag the current hour under the watermark
    and quietly disarm ``test_the_current_hour_is_included_in_every_endpoint``.
    """
    ref = datetime.now(timezone.utc).replace(minute=_CLOCK_MINUTE, second=0, microsecond=0)
    probes = {}
    with pg_migrated.begin() as conn:
        eid = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'ph8q-edge', 'PQ', true) RETURNING id"
        )).scalar()
        for period, days in BOUNDARY_PERIODS.items():
            probe = (ref - timedelta(days=days)).replace(minute=_PROBE_MINUTE)
            conn.execute(text(_INSERT.replace(":time", "CAST(:ts AS timestamptz)")), {
                "ts": probe, "eid": eid, "model": entity_usage["models"][0],
                "inp": 3, "out": 4, "cost": "0.5", "dur": 1.0, "ttft": 0.5,
            })
            probes[period] = probe

    for probe in probes.values():
        hour = probe.replace(minute=0)
        _refresh(pg_migrated, window=(hour, hour + timedelta(hours=1)))

    return {"eid": eid, "ref": ref, "probes": probes}


@pytest.fixture
def frozen_clock(monkeypatch, boundary_usage):
    """Pin ``_usage_period_start``'s clock to the fixture's mid-hour instant.

    Without this the test only catches the bug when the wall clock's minute
    happens to be below ``_PROBE_MINUTE``, i.e. half the time — a test that
    passes on a coin flip is not coverage. ``datetime.now`` is called in
    exactly one place in the module under test, so the subclass is surgical.
    """
    ref = boundary_usage["ref"]

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return ref

    from lumen.blueprints.profile import routes
    monkeypatch.setattr(routes, "datetime", _FrozenDatetime)
    return ref


@pytest.mark.postgres
@pytest.mark.parametrize("period", list(BOUNDARY_PERIODS))
def test_the_windows_first_partial_hour_is_not_dropped(
    pg_app, pg_migrated, boundary_usage, frozen_clock, period
):
    """Every per-entity endpoint, over a window that starts mid-hour.

    With an unfloored start the aggregate arm loses the probe row and each
    assertion below fails against the raw oracle it is compared to.
    """
    eid = boundary_usage["eid"]
    probe = boundary_usage["probes"][period]
    start = _period_start(pg_app, period)
    assert _covers(pg_app, start) is True, (
        "the aggregate does not cover this window, so the endpoint fell back to "
        "raw and this test would compare raw against raw"
    )

    # The probe is inside the window and inside its first hourly bucket, which
    # is what makes this test about the skew and not about anything else.
    unfloored = frozen_clock - timedelta(days=BOUNDARY_PERIODS[period])
    assert unfloored < probe < unfloored.replace(minute=0) + timedelta(hours=1)

    in_window = sum(1 for d in BOUNDARY_PERIODS.values() if d <= BOUNDARY_PERIODS[period])
    client = _client(pg_app, eid)

    summary = _raw(pg_migrated, RAW_SUMMARY, start, eid)[0]
    assert int(summary[0]) == in_window, "the oracle itself lost the probe row"
    body = _json(client, "/api/usage/summary", period)
    assert body["requests"] == int(summary[0])
    assert body["tokens"] == int(summary[1])
    assert body["cost"] == pytest.approx(float(summary[2]))

    for path, sql in (("/api/usage/requests", RAW_REQUESTS), ("/api/usage/tokens", RAW_TOKENS)):
        expected = [
            {"period": r[0].isoformat(), "count": int(r[1])}
            for r in _raw(pg_migrated, sql, start, eid, bucket=_bucket_for(period))
        ]
        assert _json(client, path, period) == expected, path
        assert expected

    _assert_models_match(
        _json(client, "/api/usage/models", period),
        _raw(pg_migrated, RAW_MODELS, start, eid, alias="rl."),
    )

    heatmap = sorted(
        (int(r[0]), int(r[1]), int(r[2]))
        for r in _raw(pg_migrated, RAW_HEATMAP, start, eid)
    )
    assert sorted((c["dow"], c["hour"], c["count"]) for c in _json(client, "/api/usage/heatmap", period)) == heatmap
    assert heatmap


# --------------------------------------------------------------------------
# 8. A hard-deleted entity.
# --------------------------------------------------------------------------

@pytest.fixture
def deleted_entity(pg_migrated, entity_usage):
    """History that is materialised in the aggregate, then its entity deleted.

    Nothing in the application does this — deleting a project flips ``active``
    — so it is the DBA-level ``DELETE FROM entities`` that ``ON DELETE SET
    NULL`` on ``request_logs.entity_id`` exists to survive. Raw rows drop to
    NULL; the aggregate materialised the id and never re-evaluates the foreign
    key, so its rows keep the old id for as long as the view lives.

    Yields ``(admin_client_entity_id, deleted_entity_id, hour)``.
    """
    ts = datetime.now(timezone.utc) - timedelta(hours=2)
    with pg_migrated.begin() as conn:
        gone = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'ph8q-gone', 'PQ', true) RETURNING id"
        )).scalar()
        admin = conn.execute(text(
            "INSERT INTO entities (entity_type, name, email, initials, active) "
            "VALUES ('user', 'ph8q-admin', 'admin@example.com', 'PQ', true) RETURNING id"
        )).scalar()
        for _ in range(3):
            conn.execute(text(_INSERT.replace(":time", "CAST(:ts AS timestamptz)")), {
                "ts": ts, "eid": gone, "model": entity_usage["models"][0],
                "inp": 5, "out": 6, "cost": "0.5", "dur": 1.0, "ttft": 0.5,
            })

    hour = ts.replace(minute=0, second=0, microsecond=0)
    _refresh(pg_migrated, window=(hour, hour + timedelta(hours=1)))

    with pg_migrated.begin() as conn:
        conn.execute(text("DELETE FROM entities WHERE id = :e"), {"e": gone})

    yield admin, gone, hour

    with pg_migrated.begin() as conn:
        conn.execute(text(
            "DELETE FROM request_logs WHERE entity_id IS NULL AND time = CAST(:ts AS timestamptz)"
        ), {"ts": ts})
        conn.execute(text("DELETE FROM entities WHERE id = :e"), {"e": admin})


@pytest.mark.postgres
def test_a_deleted_entitys_history_is_gone_from_both_arms(pg_app, pg_migrated, deleted_entity):
    """The aggregate must not resurrect what the foreign key nulled out.

    Both arms answer for an entity that no longer exists, and they have to
    answer the same thing. Without the existence clause the raw arm returns
    nothing and the aggregate arm returns the entity's full materialised
    history — so the same admin URL changes its answer the first time the
    refresh policy runs.
    """
    admin, gone, hour = deleted_entity
    with pg_migrated.connect() as conn:
        raw_rows = conn.execute(text(
            "SELECT COUNT(*) FROM request_logs WHERE entity_id = :e"
        ), {"e": gone}).scalar()
        in_view = conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {VIEW} WHERE entity_id = :e"
        ), {"e": gone}).scalar()
    assert raw_rows == 0, "ON DELETE SET NULL did not fire; this test proves nothing"
    assert int(in_view) == 3, (
        "the aggregate no longer holds the deleted entity's rows, so there is "
        "nothing here for the endpoints to leak and this test is vacuous"
    )

    start = _period_start(pg_app, "week")
    assert _covers(pg_app, start) is True, "the endpoints would fall back to raw"

    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = admin
        sess["admin_mode"] = True

    body = client.get(f"/api/usage/summary?entity_id={gone}&period=week")
    assert body.status_code == HTTPStatus.OK
    assert body.get_json()["requests"] == 0
    assert body.get_json()["tokens"] == 0
    for path in ("/api/usage/requests", "/api/usage/tokens", "/api/usage/models", "/api/usage/heatmap"):
        resp = client.get(f"{path}?entity_id={gone}&period=week")
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json() == [], path
