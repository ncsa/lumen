"""Refreshing a month window that is shorter than one bucket of the aggregate.

TimescaleDB rounds a refresh window inward to whole buckets and rejects one that
holds none with "refresh window too small" — which is what the current month's
window ``[month_start, now)`` is during its first bucket. These tests read each
aggregate's bucket width from the real TimescaleDB catalog and check that
``_refresh_months`` skips such a window instead of failing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from lumen import commands

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def engine(pg_migrated_isolated):
    return pg_migrated_isolated[1]


@pytest.mark.parametrize(("view", "width"), [
    ("request_counts_hourly", timedelta(hours=1)),
    ("request_counts_hourly_by_entity", timedelta(hours=1)),
    ("request_metrics_1m", timedelta(minutes=1)),
])
def test_bucket_width_reads_the_catalog(engine, view, width):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert commands._bucket_width(conn, view) == width


@pytest.mark.parametrize(("view", "end_offset"), [
    ("request_counts_hourly", timedelta(hours=1)),
    ("request_counts_hourly_by_entity", timedelta(hours=1)),
    ("request_metrics_1m", timedelta(minutes=1)),
    ("no_such_aggregate", timedelta(0)),
])
def test_refresh_end_offset_reads_the_policy(engine, view, end_offset):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert commands._refresh_end_offset(conn, view) == end_offset


def test_bucket_width_of_an_unknown_view_is_none(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert commands._bucket_width(conn, "no_such_aggregate") is None


# A past month, so the window is fixed whatever the wall clock says: with ``now`` passed
# explicitly this is the run at 00:34 on the 1st that failed CI with "refresh window
# too small".
MONTH_START = datetime(2025, 3, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(("view", "into_month"), [
    ("request_counts_hourly", timedelta(minutes=34)),
    ("request_metrics_1m", timedelta(seconds=30)),
])
def test_refresh_months_skips_a_window_shorter_than_one_bucket(engine, capsys, view, into_month):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        refreshed = commands._refresh_months(conn, view, MONTH_START, MONTH_START + into_month)
    assert refreshed == ["2025-03"]
    out = capsys.readouterr().out
    assert "2025-03: 0 request_logs rows ... skipped (less than one bucket old" in out
    assert "aggregate rows OK" not in out


def test_refresh_months_refreshes_a_window_of_one_bucket(engine, capsys):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        refreshed = commands._refresh_months(conn, "request_counts_hourly",
                                             MONTH_START, MONTH_START + timedelta(hours=1))
    assert refreshed == ["2025-03"]
    out = capsys.readouterr().out
    assert "aggregate rows OK" in out
    assert "skipped" not in out
