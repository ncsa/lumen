from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from monitor.buckets import (
    DEFAULT_RANGE,
    RANGES,
    STATUS_PRIORITY,
    DayGrid,
    Grid,
    bucket_size,
    bucket_statuses,
    daily_status,
    get_range,
    lumen_uptime,
    make_grid,
    model_uptime,
    probe_status,
    range_days,
    worst,
)

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 17, 17, 0)  # 12:00 in Chicago
MINUTE = timedelta(minutes=1)

# (range, bucket from the plan's "Ranges and buckets" table, bucket count at a 1m probe)
TABLE = [
    ("1h", timedelta(minutes=1), 60),
    ("12h", timedelta(minutes=3), 240),
    ("24h", timedelta(minutes=5), 288),
    ("7d", timedelta(minutes=30), 336),
    ("30d", timedelta(hours=2), 360),
]


def test_all_six_ranges_are_offered():
    assert list(RANGES) == ["1h", "12h", "24h", "7d", "30d", "1y"]
    assert RANGES["1y"].daily and not any(RANGES[k].daily for k, _, _ in TABLE)


def test_unknown_range_falls_back_to_the_default():
    assert get_range("2w").key == DEFAULT_RANGE
    assert get_range(None).key == DEFAULT_RANGE
    assert get_range("7d").key == "7d"


@pytest.mark.parametrize("key, size, count", TABLE)
def test_short_range_grid_ends_now(key, size, count):
    grid = make_grid(RANGES[key], MINUTE, NOW, CHICAGO)
    assert isinstance(grid, Grid)
    assert (grid.size, grid.count) == (size, count)
    assert grid.start + grid.count * grid.size == NOW
    assert grid.index(NOW - timedelta(seconds=1)) == count - 1
    assert grid.index(grid.start) == 0
    assert grid.index(grid.start - timedelta(seconds=1)) is None
    assert grid.index(NOW) is None


@pytest.mark.parametrize("key, size, count", TABLE)
def test_bucket_is_never_shorter_than_the_probe_cadence(key, size, count):
    web = timedelta(minutes=10)
    grid = make_grid(RANGES[key], web, NOW, CHICAGO)
    assert grid.size == max(size, web) == bucket_size(RANGES[key], web)
    assert grid.size >= web
    # The buckets still cover the whole range.
    assert grid.count * grid.size >= RANGES[key].span


def test_1y_buckets_are_local_days():
    grid = make_grid(RANGES["1y"], MINUTE, NOW, CHICAGO)
    assert isinstance(grid, DayGrid)
    assert grid.count == 365
    assert grid.days[-1] == date(2026, 10, 17) and grid.days[0] == date(2025, 10, 18)
    # 04:59 UTC on the 17th is still the 16th in Chicago.
    assert grid.index(datetime(2026, 10, 17, 4, 59)) == 363
    assert grid.index(datetime(2026, 10, 17, 5, 0)) == 364
    assert grid.times()[-1] == datetime(2026, 10, 17, 5, 0)


@pytest.mark.parametrize("key", ["1h", "12h", "24h", "7d", "30d", "1y"])
def test_worst_status_wins_and_empty_buckets_are_unknown(key):
    grid = make_grid(RANGES[key], MINUTE, NOW, CHICAGO)
    last = NOW - timedelta(seconds=1)
    points = [(last, "ok"), (last, "down"), (last, "degraded")]
    if grid.count > 1:
        first = grid.times()[0] + timedelta(seconds=1)
        points += [(first, "ok"), (first, "degraded")]
    statuses = bucket_statuses(points, grid)
    assert len(statuses) == grid.count
    assert statuses[-1] == "down"
    if grid.count > 2:
        assert statuses[0] == "degraded"
        assert set(statuses[1:-1]) == {"unknown"}


def test_points_outside_the_range_are_ignored():
    grid = make_grid(RANGES["1h"], MINUTE, NOW, CHICAGO)
    statuses = bucket_statuses([(NOW - timedelta(hours=2), "down"), (NOW + MINUTE, "down")], grid)
    assert set(statuses) == {"unknown"}


def test_worst_ranks_unknown_statuses_below_ok():
    assert STATUS_PRIORITY == {"ok": 1, "degraded": 2, "down": 3}
    assert worst([]) == "unknown"
    assert worst(["starting", "ok"]) == "ok"
    # A bucket with only statuses outside STATUS_PRIORITY is drawn as degraded and counts against uptime.
    assert worst(["starting"]) == worst(["starting", "loading"]) == "degraded"
    assert model_uptime([worst(["starting"]), worst(["ok"])]) == 50
    assert worst(["ok", "degraded"]) == "degraded"


def test_probe_status():
    assert probe_status(False, None, None) == "down"
    assert probe_status(True, 3, 2) == "degraded"
    assert probe_status(True, 3, 3) == "ok"
    assert probe_status(True, None, None) == "ok"


@pytest.mark.parametrize("polls, polls_ok, status", [
    (0, 0, "unknown"),
    (None, None, "unknown"),
    (100, 100, "ok"),
    (100, 99, "ok"),          # exactly 99% is ok
    (1000, 989, "degraded"),  # 98.9%
    (100, 1, "degraded"),
    (100, 0, "down"),
])
def test_1y_day_thresholds(polls, polls_ok, status):
    assert daily_status(polls, polls_ok) == status


def test_lumen_uptime_counts_degraded_as_up():
    assert lumen_uptime(["ok", "degraded", "down", "unknown"]) == pytest.approx(200 / 3)
    assert lumen_uptime(["degraded", "degraded"]) == 100
    assert lumen_uptime(["unknown"]) is None


def test_model_uptime_is_the_share_of_ok_buckets():
    assert model_uptime(["ok", "degraded", "down", "ok", "unknown"]) == 50
    assert model_uptime(["ok"]) == 100
    assert model_uptime(["unknown", "unknown"]) is None


def test_range_days():
    assert range_days(RANGES["7d"], NOW, CHICAGO) == [date(2026, 10, 11) + timedelta(days=i) for i in range(7)]
    assert len(range_days(RANGES["30d"], NOW, CHICAGO)) == 30
    assert range_days(RANGES["1h"], NOW, CHICAGO) == [date(2026, 10, 17)]
    # 24h back from noon crosses into yesterday.
    assert range_days(RANGES["24h"], NOW, CHICAGO) == [date(2026, 10, 16), date(2026, 10, 17)]
