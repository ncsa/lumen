"""Refreshing a month window that is shorter than one bucket of the aggregate.

TimescaleDB rounds a refresh window inward to whole buckets and rejects one that
holds none with "refresh window too small" — which is what the current month's
window ``[month_start, now)`` is during its first bucket. These tests read each
aggregate's bucket width from the real TimescaleDB catalog, the input to the
short-window check.
"""

from datetime import timedelta

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


def test_bucket_width_of_an_unknown_view_is_none(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert commands._bucket_width(conn, "no_such_aggregate") is None
