"""The migration chain, run against a real PostgreSQL + TimescaleDB.

These are the tests the SQLite suite structurally cannot be: they prove that
``request_logs`` really is a hypertable, that the continuous aggregate exists and
refreshes, and that the extension the container cannot boot without is actually
required. If someone replaces the hypertable with a plain table, at least one
test here must fail — otherwise this file is decoration.
"""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def test_timescaledb_extension_is_installed(pg_migrated):
    """The migration runs CREATE EXTENSION unconditionally on PostgreSQL.

    This is not a nice-to-have: ``entrypoint.sh`` runs ``flask db upgrade``
    before ``exec uvicorn``, so a database without the extension fails the
    migration and the container never starts.
    """
    with pg_migrated.connect() as conn:
        version = conn.execute(text(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
        )).scalar()
    assert version is not None, "timescaledb extension is not installed"


def test_request_logs_is_a_hypertable(pg_migrated):
    """The gate: this fails if request_logs is ever demoted to a plain table."""
    with pg_migrated.connect() as conn:
        row = conn.execute(text(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'request_logs'"
        )).scalar()
    assert row == "request_logs", (
        "request_logs is not a hypertable — the analytics queries, the chunk "
        "interval and any future retention/compression policy all depend on it"
    )


def test_request_logs_chunk_interval_is_seven_days(pg_migrated):
    with pg_migrated.connect() as conn:
        interval = conn.execute(text(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_name = 'request_logs' AND column_name = 'time'"
        )).scalar()
    assert interval is not None and interval.days == 7, f"unexpected chunk interval: {interval!r}"


def test_continuous_aggregate_exists(pg_migrated):
    with pg_migrated.connect() as conn:
        name = conn.execute(text(
            "SELECT view_name FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = 'request_counts_hourly'"
        )).scalar()
    assert name == "request_counts_hourly"


def test_continuous_aggregate_is_real_time(pg_migrated):
    """``materialized_only`` must be false, and it is not the 2.13+ default.

    ``i9j0k1l2m3n4`` created this view without setting it, which was real-time
    on the TimescaleDB of the day and materialised-only from 2.13 onwards —
    silently, on a version upgrade, with nothing in the migration chain to say
    so. ``j4k5l6m7n8o9`` sets it explicitly; this is the assertion that keeps it
    set, and the only one that fails on the *setting* rather than on a symptom.
    """
    with pg_migrated.connect() as conn:
        materialized_only = conn.execute(text(
            "SELECT materialized_only FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = 'request_counts_hourly'"
        )).scalar()
    assert materialized_only is False, (
        "request_counts_hourly is materialised-only: every org-wide /usage chart "
        "reads it and nothing else, so they all lose the last one to two hours"
    )


def test_continuous_aggregate_reflects_inserted_rows(pg_migrated):
    """Insert, refresh, read back — including the bucket that is still open.

    The aggregate's own policy lags real time by at least an hour, so the
    refresh has to be explicit — the same reason ``seed_analytics.py`` calls it
    directly. The first two rows are dated well into the past so they fall
    outside the policy's ``end_offset`` window and are eligible for
    materialisation.

    **The current-bucket row is the load-bearing part.** Seeded with history
    alone, this test passes whether or not the view is real-time: the org-wide
    ``/usage`` charts could be missing the last two hours of every day and the
    assertions below would still read 2 == 2. So a third row is written into the
    *current* hour, and written **after** the refresh, which puts it provably
    above the watermark where only real-time aggregation can reach it.

    The refresh window therefore ends an hour back rather than at ``NULL``:
    ``NULL`` would materialise the open bucket and move the watermark past it,
    which is precisely what switches real-time aggregation off for that bucket
    (and, since ``pg_migrated`` is shared for the session, for every later test).
    """
    with pg_migrated.begin() as conn:
        conn.execute(text("""
            INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration)
            VALUES (now() - INTERVAL '3 days', 'api', 100, 200, 0.5, 1.5),
                   (now() - INTERVAL '3 days', 'api', 300, 400, 1.5, 2.5)
        """))

    # refresh_continuous_aggregate cannot run inside a transaction block.
    with pg_migrated.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "CALL refresh_continuous_aggregate('request_counts_hourly', "
            "now() - INTERVAL '30 days', now() - INTERVAL '1 hour')"
        ))

    # Returns the row's own timestamp so the bucket can be named exactly rather
    # than by re-reading the clock and racing an hour boundary.
    with pg_migrated.begin() as conn:
        current = conn.execute(text("""
            INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration)
            VALUES (now(), 'api', 700, 800, 2.5, 3.5)
            RETURNING time
        """)).scalar()

    with pg_migrated.connect() as conn:
        requests, in_tok, out_tok = conn.execute(text(
            "SELECT SUM(requests), SUM(input_tokens), SUM(output_tokens) "
            "FROM request_counts_hourly"
        )).one()
        current_requests = conn.execute(text(
            "SELECT SUM(requests) FROM request_counts_hourly "
            "WHERE bucket = time_bucket('1 hour', CAST(:t AS TIMESTAMPTZ))"
        ), {"t": current}).scalar()

    assert current_requests == 1, (
        "the request made in the current hour is invisible through the aggregate — "
        "materialized_only is true, so the org-wide /usage charts read zero for it"
    )
    assert requests == 3
    assert in_tok == 1100
    assert out_tok == 1400


def test_migration_is_at_a_single_head(pg_migrated):
    """A merge that leaves two heads makes `db upgrade` ambiguous in production."""
    with pg_migrated.connect() as conn:
        heads = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    assert len(heads) == 1, f"expected one alembic head, got {heads}"
