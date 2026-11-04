"""``_usage_period_start``: the window start every /usage chart shares.

``year`` reads ``request_counts_daily`` org-wide, so its start must be a UTC
midnight; the shorter windows only need the hour. ``all`` has no start.
"""

from datetime import datetime, timezone

import pytest

from lumen.blueprints.profile import routes

NOW = datetime(2026, 11, 4, 13, 47, 12, 345678, tzinfo=timezone.utc)


@pytest.fixture
def frozen(monkeypatch):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(routes, "datetime", _Frozen)


def test_week_and_month_are_floored_to_the_hour(frozen):
    assert routes._usage_period_start("week") == datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    assert routes._usage_period_start("month") == datetime(2026, 10, 5, 13, tzinfo=timezone.utc)


def test_year_is_floored_to_the_utc_day(frozen):
    assert routes._usage_period_start("year") == datetime(2025, 11, 4, tzinfo=timezone.utc)


def test_all_has_no_start(frozen):
    assert routes._usage_period_start("all") is None


@pytest.mark.parametrize("period, view", [
    ("week", "request_counts_hourly"),
    ("month", "request_counts_hourly"),
    ("year", "request_counts_daily"),
    ("all", "request_counts_daily"),
    ("bogus", "request_counts_hourly"),
])
def test_org_wide_view_per_period(period, view):
    assert routes._org_usage_view(period) == view
