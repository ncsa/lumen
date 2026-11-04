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

# Built on request_logs. request_counts_hourly is built on this one and reads only its
# materialised rows, so refreshing hourly alone would materialise nothing here.
AGGREGATE = "request_counts_hourly_by_entity"
# request_logs.source is VARCHAR(8) and every usage aggregate groups by it, so the
# tag isolates this module's rows in the aggregates as well as in the raw table.
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


def _materialised_requests(engine, start, view=AGGREGATE):
    with engine.connect() as conn:
        mat = conn.execute(text(
            "SELECT materialization_hypertable_schema || '.' || materialization_hypertable_name "
            "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
        ), {"v": view}).scalar()
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
    """The seed refreshes every usage aggregate bottom-up, so the row reaches all three.

    The window is a whole UTC day: request_counts_daily rejects a window holding no
    whole bucket.
    """
    hour, _ = _hour_with_one_row(engine, days_ago=6)
    start = hour.replace(hour=0)
    end = start + timedelta(days=1)
    assert start.tzinfo is None and end.tzinfo is None
    for view in seed_analytics.USAGE_AGGREGATES:
        assert _materialised_requests(engine, hour, view) == 0

    seed_analytics.refresh_usage_aggregates(engine, start, end)

    assert _materialised_requests(engine, hour, "request_counts_hourly_by_entity") == 1
    assert _materialised_requests(engine, hour, "request_counts_hourly") == 1
    assert _materialised_requests(engine, start, "request_counts_daily") == 1
