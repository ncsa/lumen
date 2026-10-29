"""``refresh_continuous_aggregate`` called with *naive* UTC window bounds.

The procedure declares its window arguments as ``"any"``, and psycopg 3 binds
server-side with a type taken from the Python value: a naive ``datetime`` goes
out as ``timestamp``, not the ``timestamptz`` that ``request_logs.time`` and every
aggregate bucket are. ``_refresh_window`` and ``seed_analytics.py`` therefore cast
the bounds in the SQL. These tests run the real statements with naive bounds and
check the result in the materialisation hypertable, not through the view: the
view is ``materialized_only = false`` and would show the row through real-time
aggregation whether or not the refresh did anything.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

import seed_analytics
from lumen import commands
from lumen.timeutils import utcnow

pytestmark = pytest.mark.postgres

AGGREGATE = "request_counts_hourly"
# request_logs.source is VARCHAR(8) and request_counts_hourly groups by it, so the
# tag isolates this module's rows in the aggregate as well as in the raw table.
SOURCE = "naivetst"


@pytest.fixture(scope="module")
def engine(pg_migrated_isolated):
    return pg_migrated_isolated[1]


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _hour_with_one_row(engine, days_ago):
    """Insert one tagged row inside a past hour; return that hour as naive UTC bounds."""
    start = (utcnow() - timedelta(days=days_ago)).replace(minute=0, second=0, microsecond=0)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (CAST(:t AS timestamp) AT TIME ZONE 'UTC', :s, 1, 1, 0.01, 0.1)"
        ), {"t": start + timedelta(minutes=10), "s": SOURCE})
    return start, start + timedelta(hours=1)


def _materialised_requests(engine, start):
    with engine.connect() as conn:
        mat = conn.execute(text(
            "SELECT materialization_hypertable_schema || '.' || materialization_hypertable_name "
            "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
        ), {"v": AGGREGATE}).scalar()
        return conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {mat} "
            "WHERE source = :s AND bucket = CAST(:b AS timestamp) AT TIME ZONE 'UTC'"
        ), {"s": SOURCE, "b": start}).scalar()


def test_refresh_window_materialises_a_naive_utc_window(engine):
    start, end = _hour_with_one_row(engine, days_ago=5)
    assert start.tzinfo is None and end.tzinfo is None
    assert _materialised_requests(engine, start) == 0

    with _autocommit(engine) as conn:
        assert commands._refresh_window(conn, AGGREGATE, start, end) == 0

    assert _materialised_requests(engine, start) == 1


def test_seed_refresh_materialises_a_naive_utc_window(engine):
    start, end = _hour_with_one_row(engine, days_ago=6)
    assert start.tzinfo is None and end.tzinfo is None
    assert _materialised_requests(engine, start) == 0

    seed_analytics.refresh_hourly_aggregate(engine, start, end)

    assert _materialised_requests(engine, start) == 1
