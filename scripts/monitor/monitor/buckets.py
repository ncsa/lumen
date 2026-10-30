"""Dashboard ranges, time buckets, bucket status and uptime.

The short ranges (1h to 30d) bucket the raw `probes` and `samples` rows into
equal slots ending now; a bucket is never shorter than `refresh.web`, so it
always has room for a probe. The 1y range uses one bucket per local day from
the daily rows. A bucket takes the worst status of what fell into it
(`STATUS_PRIORITY`); a bucket with nothing in it is `unknown`.

`bucket_index()` and `status_rank()` do the same bucketing in SQL, so a query
can `GROUP BY` bucket and return one row per bucket instead of every row.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import BigInteger, case
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement, FunctionElement, literal

from monitor.config import DEFAULT_DOWN_ABOVE
from monitor.daily import local_day, local_midnight

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

    A status Lumen reports that is not in `STATUS_PRIORITY` (e.g. `starting`)
    ranks below ok; a bucket with only such statuses is degraded, so the strip
    draws what uptime counts: the model reported, but was not ok.
    """
    found = list(statuses)
    if not found:
        return UNKNOWN
    result = max(found, key=lambda s: STATUS_PRIORITY.get(s, 0))
    return result if result in STATUS_PRIORITY else DEGRADED


def bucket_statuses(points, grid: Grid | DayGrid) -> list[str]:
    """Worst status per bucket of `(time, status)` points; points outside the grid are ignored."""
    buckets: list[list[str]] = [[] for _ in range(grid.count)]
    for at, status in points:
        i = grid.index(at)
        if i is not None:
            buckets[i].append(status)
    return [worst(b) for b in buckets]


EPOCH = datetime(1970, 1, 1)
MICROSECOND = timedelta(microseconds=1)


class epoch_us(FunctionElement):
    """Microseconds since the Unix epoch of a naive UTC timestamp column, as an exact integer.

    PostgreSQL: `extract(epoch …)`, which is exact (numeric) from PostgreSQL 14 on and
    rounds back to the microsecond before that. SQLite: `julianday()` only keeps
    milliseconds, and as a float, so whole seconds come from `strftime('%s')` and the
    microseconds from the stored text (`YYYY-MM-DD HH:MM:SS.ffffff`, as SQLAlchemy writes it).
    """

    type = BigInteger()
    inherit_cache = True


@compiles(epoch_us)
def _epoch_us_default(element, compiler, **kw):
    column = compiler.process(element.clauses, **kw)
    return f"CAST(round(extract(epoch FROM {column}) * 1000000) AS BIGINT)"


@compiles(epoch_us, "sqlite")
def _epoch_us_sqlite(element, compiler, **kw):
    column = compiler.process(element.clauses, **kw)
    return (f"(CAST(strftime('%s', {column}) AS INTEGER) * 1000000"
            f" + CAST(substr({column}, 21, 6) AS INTEGER))")


def bucket_index(column, grid: Grid) -> ColumnElement:
    """SQL for `grid.index(column)`: `floor((column - grid.start) / grid.size)`, in whole microseconds.

    Integer arithmetic, so a row exactly on a bucket edge lands where `Grid.index`
    puts it. Only valid for rows at or after `grid.start` (filter on the column
    first), and the result is not bounded: callers drop indexes ≥ `grid.count`.
    """
    start = (grid.start - EPOCH) // MICROSECOND
    size = grid.size // MICROSECOND
    return (epoch_us(column) - literal(start, BigInteger)) // literal(size, BigInteger)


def status_rank(column) -> ColumnElement:
    """SQL for a status's `STATUS_PRIORITY`, 0 for any other status; `MAX()` of it is a bucket's worst."""
    return case(*((column == s, p) for s, p in STATUS_PRIORITY.items()), else_=0)


RANK_STATUS = {p: s for s, p in STATUS_PRIORITY.items()}


def rank_status(rank: int | None) -> str:
    """The status for a bucket's `MAX(status_rank())`, the same as `worst()` of its statuses.

    None (no rows) is unknown; 0 (only statuses outside `STATUS_PRIORITY`) is degraded.
    """
    if rank is None:
        return UNKNOWN
    return RANK_STATUS.get(rank, DEGRADED)


def probe_status(success: bool, model_count: int | None, models_up: int | None, down_above: float = DEFAULT_DOWN_ABOVE) -> str:
    """A probe's Lumen status: down when it failed or more than `down_above` percent of the
    models it listed were down, degraded when at least one was, ok otherwise.

    `models_up` counts the models Lumen did not report `down`; a model with only some
    servers down is degraded and counts as up. Per-model statuses never go through here.
    """
    if not success:
        return DOWN
    if model_count is None or models_up is None or models_up >= model_count:
        return OK
    if (model_count - models_up) * 100 > down_above * model_count:
        return DOWN
    return DEGRADED


def lumen_daily_status(polls: int, polls_up: int, polls_ok: int) -> str:
    """1y Lumen status from a day's probes counted by `probe_status`: unknown without probes,
    down when none was up, ok when ≥ 99% were ok, degraded otherwise."""
    if not polls:
        return UNKNOWN
    if not polls_up:
        return DOWN
    if polls_ok / polls >= DAILY_OK:
        return OK
    return DEGRADED


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
    """Percent of buckets with data whose status is not down (see `probe_status`): degraded counts as up."""
    known = [s for s in statuses if s != UNKNOWN]
    if not known:
        return None
    return 100 * sum(s != DOWN for s in known) / len(known)
