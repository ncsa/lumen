"""Dashboard ranges, time buckets, bucket status and uptime.

The short ranges (1h to 30d) bucket the raw `probes` and `samples` rows into
equal slots ending now; a bucket is never shorter than `refresh.web`, so it
always has room for a probe. The 1y range uses one bucket per local day from
the daily rows. A bucket takes the worst status of what fell into it
(`STATUS_PRIORITY`); a bucket with nothing in it is `unknown`.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from monitor.daily import local_day
from monitor.totals import local_midnight

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
UNKNOWN = "unknown"
STATUS_PRIORITY = {OK: 1, DEGRADED: 2, DOWN: 3}
# 1y: a day is ok at this availability (polls_ok / polls) or more.
DAILY_OK = 0.99


@dataclass(frozen=True)
class Range:
    key: str
    label: str
    span: timedelta
    bucket: timedelta  # the nominal bucket; see bucket_size()
    daily: bool = False  # bucketed per local day from daily_stats


RANGES = {
    r.key: r
    for r in (
        Range("1h", "Last hour", timedelta(hours=1), timedelta(minutes=1)),
        Range("12h", "Last 12 hours", timedelta(hours=12), timedelta(minutes=3)),
        Range("24h", "Last 24 hours", timedelta(hours=24), timedelta(minutes=5)),
        Range("7d", "Last 7 days", timedelta(days=7), timedelta(minutes=30)),
        Range("30d", "Last 30 days", timedelta(days=30), timedelta(hours=2)),
        Range("1y", "Last year", timedelta(days=365), timedelta(days=1), daily=True),
    )
}
DEFAULT_RANGE = "24h"


def get_range(key: str | None) -> Range:
    """The range for `key`, or the default range for a missing or unknown key."""
    return RANGES.get(key or "", RANGES[DEFAULT_RANGE])


def bucket_size(rng: Range, web: timedelta) -> timedelta:
    """The range's bucket, but never shorter than the probe cadence `web`."""
    return max(rng.bucket, web)


@dataclass(frozen=True)
class Grid:
    """`count` equal buckets of `size` starting at `start` (naive UTC) and ending at `start + count × size`."""

    start: datetime
    size: timedelta
    count: int

    def index(self, at: datetime) -> int | None:
        i = math.floor((at - self.start) / self.size)
        return i if 0 <= i < self.count else None

    def times(self) -> list[datetime]:
        return [self.start + i * self.size for i in range(self.count)]


class DayGrid:
    """One bucket per local day in `tz`, oldest first; the same interface as `Grid`."""

    def __init__(self, days: list[date], tz):
        self.days = days
        self.tz = tz
        self.count = len(days)
        self._index = {d: i for i, d in enumerate(days)}

    def index(self, at: datetime) -> int | None:
        return self._index.get(local_day(at, self.tz))

    def times(self) -> list[datetime]:
        """Each day's local midnight as naive UTC."""
        return [local_midnight(d, self.tz) for d in self.days]


def make_grid(rng: Range, web: timedelta, now: datetime, tz) -> Grid | DayGrid:
    """The buckets of `rng` ending at `now`: equal slots of `bucket_size()`, or local days for 1y."""
    if rng.daily:
        return DayGrid(range_days(rng, now, tz), tz)
    size = bucket_size(rng, web)
    count = max(1, math.ceil(rng.span / size))
    return Grid(now - count * size, size, count)


def range_days(rng: Range, now: datetime, tz) -> list[date]:
    """The local days a range covers, oldest first, ending today.

    7d, 30d and 1y are that many whole days; the shorter ranges cover every
    local day they touch (usually today, or yesterday and today).
    """
    today = local_day(now, tz)
    if rng.span >= timedelta(days=7):
        count = rng.span.days
    else:
        count = (today - local_day(now - rng.span, tz)).days + 1
    return [today - timedelta(days=i) for i in range(count - 1, -1, -1)]


def worst(statuses) -> str:
    """The worst of `statuses` by `STATUS_PRIORITY`; `unknown` when there are none.

    A status Lumen reports that is not in `STATUS_PRIORITY` ranks below ok.
    """
    found = list(statuses)
    if not found:
        return UNKNOWN
    return max(found, key=lambda s: STATUS_PRIORITY.get(s, 0))


def bucket_statuses(points, grid: Grid | DayGrid) -> list[str]:
    """Worst status per bucket of `(time, status)` points; points outside the grid are ignored."""
    buckets: list[list[str]] = [[] for _ in range(grid.count)]
    for at, status in points:
        i = grid.index(at)
        if i is not None:
            buckets[i].append(status)
    return [worst(b) for b in buckets]


def probe_status(success: bool, model_count: int | None, models_up: int | None) -> str:
    """A probe's status for the Lumen strip: down when it failed, degraded when a model was down."""
    if not success:
        return DOWN
    if model_count is not None and models_up is not None and models_up < model_count:
        return DEGRADED
    return OK


def daily_status(polls: int | None, polls_ok: int | None) -> str:
    """1y bucket status from a day's counters: ok at ≥ 99% availability, degraded above 0%, else down."""
    if not polls:
        return UNKNOWN
    availability = (polls_ok or 0) / polls
    if availability >= DAILY_OK:
        return OK
    if availability > 0:
        return DEGRADED
    return DOWN


def model_uptime(statuses: list[str]) -> float | None:
    """Percent of buckets with data whose status is ok; None when no bucket has data."""
    known = [s for s in statuses if s != UNKNOWN]
    if not known:
        return None
    return 100 * sum(s == OK for s in known) / len(known)


def lumen_uptime(statuses: list[str]) -> float | None:
    """Percent of buckets with data that had no failed probe: degraded counts as up."""
    known = [s for s in statuses if s != UNKNOWN]
    if not known:
        return None
    return 100 * sum(s != DOWN for s in known) / len(known)
