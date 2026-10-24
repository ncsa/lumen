"""Phase 8 lifecycle migrations, against a real PostgreSQL + TimescaleDB.

Three things are being proven here, and only the first is obvious.

1. The two new continuous aggregates exist and carry the measures the per-user
   and per-model pages will read once raw ``request_logs`` chunks start being
   dropped.

2. **They return the CURRENT bucket.** ``timescaledb.materialized_only``
   defaults to ``true`` on the deployed 2.27.2, and a default aggregate answers
   nothing newer than its last materialisation — up to two hours ago for an
   hourly view with an hourly schedule. Every equality test below therefore
   seeds a row inside the *current* bucket as well as historical ones. Seeded
   with history alone these tests pass at 0 == 0 while certifying the exact
   regression the phase exists to prevent, so the current-bucket row is the
   single most load-bearing line in this file. The ``seeded`` fixture
   materialises the history first and only then writes the current bucket, so
   those rows are provably above the watermark and reachable only through
   real-time aggregation.

3. Compression is a lossless round trip. The trap there is that
   ``show_chunks(older_than => ...)`` silently matches nothing when every seeded
   row lands in one still-open chunk, so "results are identical before and
   after" would hold trivially without a byte having been compressed. The test
   asserts a chunk actually became compressed before it draws any conclusion.

This module runs against ``pg_migrated_isolated`` — a database of its own. It is
the one that moves watermarks and drops aggregates out from under everything
else, so it isolates too; see that fixture's docstring.
"""

import time as _time

import pytest
from sqlalchemy import exc, text

pytestmark = pytest.mark.postgres

SOURCE = "ph8"
VIEWS = ["request_counts_hourly_by_entity", "request_metrics_1m"]


@pytest.fixture(scope="module")
def pg_url(pg_migrated_isolated):
    return pg_migrated_isolated[0]


@pytest.fixture(scope="module")
def pg_migrated(pg_migrated_isolated):
    return pg_migrated_isolated[1]


# The aggregates and the raw table, reduced to one comparable tuple each. Used
# for the compression before/after comparison.
_HOURLY_TOTALS = f"""
    SELECT SUM(requests), SUM(input_tokens), SUM(output_tokens), SUM(cost),
           SUM(duration_sum), MAX(duration_max), SUM(aborts),
           SUM(ttft_count), SUM(ttft_sum), MAX(ttft_max),
           SUM(ttft_le_0_5), SUM(ttft_le_1), SUM(ttft_le_2),
           SUM(ttft_le_5), SUM(ttft_le_10), SUM(ttft_le_30)
    FROM request_counts_hourly_by_entity WHERE source = '{SOURCE}'
"""

_MINUTE_TOTALS = f"""
    SELECT SUM(requests), SUM(input_tokens), SUM(output_tokens), SUM(cost),
           SUM(duration_sum), MAX(duration_max), SUM(aborts),
           SUM(ttft_count), SUM(ttft_sum), MAX(ttft_max),
           SUM(ttft_le_0_5), SUM(ttft_le_1), SUM(ttft_le_2),
           SUM(ttft_le_5), SUM(ttft_le_10), SUM(ttft_le_30)
    FROM request_metrics_1m WHERE source = '{SOURCE}'
"""

_INSERT = f"""
    INSERT INTO request_logs
        (time, source, input_tokens, output_tokens, cost, duration,
         ttft_visible, outcome, aborted)
    VALUES (:time, '{SOURCE}', :inp, :out, :cost, :dur, :ttft, :outcome, :aborted)
"""

# Historical rows, spread across three separate 7-day chunks so the compression
# test has something older than its 7-day threshold to work on. ttft_visible
# values are chosen to land one in each cumulative bucket band.
_HISTORY = [
    dict(age="40 days", inp=1, out=2, cost="0.1", dur=1.0, ttft=0.25, outcome="ok", aborted=False),
    dict(age="20 days", inp=3, out=4, cost="0.2", dur=2.0, ttft=1.5, outcome="disconnect", aborted=False),
    # outcome 'ok' but aborted true: `aborts` must follow `outcome`, not the
    # older boolean column, or the two counters disagree for every such row.
    dict(age="9 days", inp=5, out=6, cost="0.3", dur=3.0, ttft=7.0, outcome="ok", aborted=True),
]

# Rows in the current hour/minute bucket. The NULL ttft_visible must count in
# ttft_count and in none of the ttft_le_* buckets.
_CURRENT = [
    dict(inp=7, out=8, cost="0.4", dur=4.0, ttft=None, outcome="ok", aborted=False),
    dict(inp=9, out=10, cost="0.5", dur=5.0, ttft=25.0, outcome="disconnect", aborted=False),
]


@pytest.fixture(scope="module")
def seeded(pg_migrated):
    """Seed history, materialise it, then seed the current bucket.

    The order is the whole point, and it is the order production will be in.

    History is written first and then materialised with an explicit full
    refresh — what the operator backfill command does. That refresh is not
    convenience: it advances the aggregate's watermark to the end of the last
    complete bucket, and **a row below the watermark that was never
    materialised is invisible**, because real-time aggregation only scans raw
    rows *above* it. Without the refresh, the scheduled policy (30 days for the
    hourly view, 3 hours for the minute view) would materialise its own window,
    move the watermark past everything older, and silently drop the older rows
    from every query here — non-deterministically, depending on whether the
    background job happened to have fired yet.

    The current-bucket rows are then written *after* the refresh, so they sit
    above the watermark and can only be seen through real-time aggregation.
    That makes every assertion about them a direct test of
    ``materialized_only = false``: with the 2.27 default they would be gone.

    Yields the timestamp the current-bucket rows were written with, so the
    tests can name that bucket exactly rather than re-reading the clock and
    racing an hour boundary.
    """
    with pg_migrated.begin() as conn:
        # request_metrics_1m keeps 30 days (#169) and the oldest history row is
        # 40 days old. Pause that policy so its job cannot drop the row's bucket
        # partway through the module, depending on when it happens to fire. The
        # pause is never undone: this database is private to the module.
        conn.execute(text(
            "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention'"
        ))
        for row in _HISTORY:
            params = {k: v for k, v in row.items() if k != "age"}
            conn.execute(text(_INSERT.replace(":time", f"now() - INTERVAL '{row['age']}'")), params)

    for view in VIEWS:
        _refresh(pg_migrated, view)

    with pg_migrated.begin() as conn:
        now = conn.execute(text(
            _INSERT.replace(":time", "now()") + " RETURNING time"
        ), _CURRENT[0]).scalar()
        conn.execute(text(_INSERT), {**_CURRENT[1], "time": now})

    yield now

    with pg_migrated.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # A DELETE would otherwise have to reach into a chunk the compression
        # test compressed.
        conn.execute(text("SELECT decompress_chunk(c, true) FROM show_chunks('request_logs') c"))
    with pg_migrated.begin() as conn:
        conn.execute(text(f"DELETE FROM request_logs WHERE source = '{SOURCE}'"))


def _refresh(engine, view, deadline=60.0):
    """Materialise a view's full history, retrying while a policy job holds it.

    The aggregates carry background refresh policies — one runs every minute —
    and TimescaleDB refuses an overlapping refresh outright with
    ``LockNotAvailable`` rather than waiting. Nothing about that is specific to
    tests: an operator backfill hits the same wall. Retrying is the whole
    remedy, since the policy's own window is small and finishes quickly.
    """
    end = _time.monotonic() + deadline
    while True:
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)"))
            return
        except exc.OperationalError as err:
            if "concurrent refresh" not in str(err) or _time.monotonic() > end:
                raise
            _time.sleep(1)


def _materialized_only(conn, view):
    return conn.execute(text(
        "SELECT materialized_only FROM timescaledb_information.continuous_aggregates "
        "WHERE view_name = :v"
    ), {"v": view}).scalar()


@pytest.mark.parametrize("view", VIEWS)
def test_aggregate_exists_and_is_real_time(pg_migrated, view):
    """The assertion this whole phase turns on.

    ``materialized_only = true`` is the 2.27 default and it hides the current
    bucket. A per-user chart that silently omits the last hour of everybody's
    activity is a worse and far more frequent complaint than the retention
    truncation these aggregates exist to prevent.
    """
    with pg_migrated.connect() as conn:
        assert _materialized_only(conn, view) is False, (
            f"{view} is materialized_only; it will not return the current bucket"
        )


def test_hourly_aggregate_includes_the_current_bucket(pg_migrated, seeded):
    """Raw and aggregate totals agree, current bucket included.

    Both halves matter. The current-bucket rows were written after the seed's
    refresh, so they are above the watermark and reachable only through
    real-time aggregation: with ``materialized_only = true`` this test sees the
    materialised history and misses them. Seeded with history alone it would
    pass either way, certifying the exact regression the phase prevents.
    """
    with pg_migrated.connect() as conn:
        raw = conn.execute(text(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost), SUM(duration) "
            f"FROM request_logs WHERE source = '{SOURCE}'"
        )).one()
        agg = conn.execute(text(
            "SELECT SUM(requests), SUM(input_tokens), SUM(output_tokens), SUM(cost), SUM(duration_sum) "
            f"FROM request_counts_hourly_by_entity WHERE source = '{SOURCE}'"
        )).one()
        current = conn.execute(text(
            f"SELECT SUM(requests) FROM request_counts_hourly_by_entity "
            f"WHERE source = '{SOURCE}' AND bucket = date_trunc('hour', CAST(:t AS timestamptz))"
        ), {"t": seeded}).scalar()

    assert tuple(agg) == tuple(raw), "aggregate disagrees with the raw table"
    assert current == len(_CURRENT), (
        "the current hour bucket is missing from request_counts_hourly_by_entity — "
        "this is what materialized_only = true looks like"
    )


def test_minute_aggregate_includes_the_current_bucket(pg_migrated, seeded):
    with pg_migrated.connect() as conn:
        raw = conn.execute(text(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost), SUM(duration) "
            f"FROM request_logs WHERE source = '{SOURCE}'"
        )).one()
        agg = conn.execute(text(
            "SELECT SUM(requests), SUM(input_tokens), SUM(output_tokens), SUM(cost), SUM(duration_sum) "
            f"FROM request_metrics_1m WHERE source = '{SOURCE}'"
        )).one()
        current = conn.execute(text(
            f"SELECT SUM(requests) FROM request_metrics_1m "
            f"WHERE source = '{SOURCE}' AND bucket = date_trunc('minute', CAST(:t AS timestamptz))"
        ), {"t": seeded}).scalar()

    assert tuple(agg) == tuple(raw)
    assert current == len(_CURRENT), "the current minute bucket is missing from request_metrics_1m"


def _hourly_columns(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'request_counts_hourly_by_entity'"
        )).scalars().all()


def test_minute_aggregate_has_no_entity_dimension(pg_migrated):
    """Stated in the migration and worth a guard: adding it multiplies rows by class size."""
    with pg_migrated.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'request_metrics_1m'"
        )).scalars().all()
    assert "entity_id" not in cols
    assert "entity_id" in _hourly_columns(pg_migrated)


@pytest.mark.parametrize("view", VIEWS)
def test_ttft_buckets_are_cumulative_and_exclude_nulls(pg_migrated, seeded, view):
    """The ttft_le_* columns are a Prometheus-style cumulative histogram.

    Non-decreasing across the edges, bounded above by the non-null count, and a
    NULL ``ttft_visible`` (a row written before the timing columns existed)
    counts in none of them — otherwise every historical bucket reads as fast.
    """
    with pg_migrated.connect() as conn:
        counts = conn.execute(text(
            "SELECT SUM(ttft_count), SUM(ttft_le_0_5), SUM(ttft_le_1), SUM(ttft_le_2), "
            "       SUM(ttft_le_5), SUM(ttft_le_10), SUM(ttft_le_30) "
            f"FROM {view} WHERE source = '{SOURCE}'"
        )).one()
        non_null = conn.execute(text(
            f"SELECT COUNT(ttft_visible) FROM request_logs WHERE source = '{SOURCE}'"
        )).scalar()

    ttft_count, *buckets = [int(v) for v in counts]
    assert ttft_count == non_null, "ttft_count must be the non-null denominator"
    assert buckets == sorted(buckets), f"ttft_le_* is not monotonically non-decreasing: {buckets}"
    assert buckets[-1] <= ttft_count, "a NULL ttft_visible was counted in a bucket"
    # One NULL row was seeded; the widest bucket must fall short of the row count by it.
    assert buckets[-1] == len(_HISTORY) + len(_CURRENT) - 1


@pytest.mark.parametrize("view", VIEWS)
def test_aborts_counts_only_the_disconnect_outcome(pg_migrated, seeded, view):
    """`aborts` follows `outcome`, not the older `aborted` boolean.

    The seed deliberately includes a row with ``outcome = 'ok'`` and
    ``aborted = true``; counting that one would make the two disagree.
    """
    with pg_migrated.connect() as conn:
        aborts = conn.execute(text(
            f"SELECT SUM(aborts) FROM {view} WHERE source = '{SOURCE}'"
        )).scalar()
        expected = conn.execute(text(
            f"SELECT COUNT(*) FROM request_logs WHERE source = '{SOURCE}' AND outcome = 'disconnect'"
        )).scalar()
        aborted_flag = conn.execute(text(
            f"SELECT COUNT(*) FROM request_logs WHERE source = '{SOURCE}' AND aborted"
        )).scalar()

    assert int(aborts) == expected == 2
    assert aborted_flag != expected, "the seed no longer distinguishes `aborted` from `outcome`"


def test_compression_is_enabled_with_a_seven_day_policy(pg_migrated):
    with pg_migrated.connect() as conn:
        enabled = conn.execute(text(
            "SELECT compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'request_logs'"
        )).scalar()
        policies = conn.execute(text(
            "SELECT config FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_name = 'request_logs'"
        )).scalars().all()

    assert enabled is True
    assert len(policies) == 1, f"expected exactly one compression policy, got {policies}"
    assert policies[0]["compress_after"] == "7 days"


def test_compression_round_trip_leaves_query_results_identical(pg_migrated, seeded):
    """Compress real chunks and prove nothing changed.

    The assertion that a chunk actually became compressed is not decoration:
    ``show_chunks(older_than => ...)`` returns an empty set when every seeded
    row sits in the current, still-open chunk, and the equality below would
    then hold without a single byte having been compressed.
    """
    with pg_migrated.connect() as conn:
        before_hourly = conn.execute(text(_HOURLY_TOTALS)).one()
        before_minute = conn.execute(text(_MINUTE_TOTALS)).one()

    with pg_migrated.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        chunks = conn.execute(text(
            "SELECT show_chunks('request_logs', older_than => INTERVAL '7 days')"
        )).scalars().all()
        for chunk in chunks:
            conn.execute(text("SELECT compress_chunk(:c, if_not_compressed => true)"), {"c": chunk})

    with pg_migrated.connect() as conn:
        compressed = conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND is_compressed"
        )).scalar()
        after_hourly = conn.execute(text(_HOURLY_TOTALS)).one()
        after_minute = conn.execute(text(_MINUTE_TOTALS)).one()

    assert compressed >= 1, (
        "nothing was actually compressed — every seeded row is inside the open "
        "chunk, so this test proved nothing about compression"
    )
    assert tuple(after_hourly) == tuple(before_hourly)
    assert tuple(after_minute) == tuple(before_minute)


def test_add_column_still_works_on_a_compressed_hypertable(pg_migrated):
    """Compression must not be a one-way door for later schema work.

    Measured on 2.27.2: nullable ``ADD COLUMN`` succeeds against compressed
    chunks. If a version bump ever changes that, the next phase to add a column
    finds out here rather than in production.
    """
    with pg_migrated.begin() as conn:
        conn.execute(text("ALTER TABLE request_logs ADD COLUMN phase8_probe FLOAT"))
        conn.execute(text("ALTER TABLE request_logs DROP COLUMN phase8_probe"))


def test_the_only_retention_policy_is_on_request_metrics_1m(pg_migrated):
    """Deliberate. `entrypoint.sh` runs `flask db upgrade` at container start,
    so a migration that added retention to `request_logs` would start deleting
    production data on the next deploy. The one policy any migration adds keeps
    30 days of the minute aggregate (#169)."""
    with pg_migrated.connect() as conn:
        jobs = conn.execute(text(
            "SELECT hypertable_name FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention'"
        )).scalars().all()
    assert jobs == ["request_metrics_1m"], f"unexpected retention policies: {jobs}"
