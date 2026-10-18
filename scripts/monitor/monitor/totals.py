"""The models run: restart-safe daily request and token totals.

The run makes no HTTP call. It reads the latest successful probe snapshot
(skipped when it is older than 2 × `refresh.web`) and adds the traffic since
the metric's watermark to each model's lumen daily row, then moves the
watermark to the snapshot time. Requests and tokens are independently
optional in Lumen's response, so each has its own watermark (`covered_until`
for requests, `tokens_covered_until` for tokens) and is planned on its own.
With `gap = snapshot time − watermark`:

- `gap ≤ 1h`: add `last_hour × gap/1h`.
- `gap ≤ 24h`: add `last_day × gap/24h` and mark the rows `estimated`.
- longer, with `last_month`: add `last_month × gap/30d` (at most 30 days back)
  and fill the missed days with `estimated` rows. Without `last_month`, go
  back at most 24h with `last_day` and leave the missed days empty.

A span that crosses local midnight is split by duration between the days.
A watermark only moves when its metric was counted: a snapshot without a
usable window for a metric defers that metric's gap to the next snapshot that
has one. The watermarks live in the database, so a restart picks up where the
last run stopped and never counts the same time twice. A metric seen for the
first time is counted from local midnight.

After the totals, lumen rows of earlier days are marked `final`, and final
rows never change again. A day closes once every metric that has a watermark
for the model is counted past its end, so the first run after midnight
normally closes yesterday. A day whose traffic is still deferred stays open
until a window covers it, or until it is 30 days old and no Lumen window can
reach it any more. Days backfilled by this run are closed in the same run:
Lumen's windows never describe them more precisely later, so they stay
`estimated`.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from monitor.config import ConfigStore
from monitor.daily import get_daily, local_day, upsert_daily
from monitor.db import utcnow
from monitor.models import DailyStat
from monitor.probe import LiveSnapshot

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
MONTH = timedelta(days=30)
# Each metric and the column holding how far it is counted.
METRICS = {"requests": "covered_until", "tokens": "tokens_covered_until"}


@dataclass(frozen=True)
class TotalsResult:
    skipped: str | None = None
    models: int = 0
    finalized: int = 0


@dataclass(frozen=True)
class Span:
    """Traffic at `rate` per second from `start` to the snapshot time."""

    start: datetime
    rate: float
    estimated: bool


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def plan_span(windows: dict | None, covered_until: datetime, end: datetime) -> Span | None:
    """Which window covers `covered_until..end`, or None when Lumen reports none that fits."""
    if not windows:
        return None
    gap = end - covered_until
    hour, day, month = (_number(windows.get(key)) for key in ("last_hour", "last_day", "last_month"))
    if gap <= HOUR and hour is not None:
        return Span(covered_until, hour / HOUR.total_seconds(), False)
    if gap <= DAY and day is not None:
        return Span(covered_until, day / DAY.total_seconds(), True)
    if month is not None:
        return Span(max(covered_until, end - MONTH), month / MONTH.total_seconds(), True)
    if day is not None:
        return Span(max(covered_until, end - DAY), day / DAY.total_seconds(), True)
    return None


def local_midnight(day: date, tz) -> datetime:
    """Local midnight starting `day`, as naive UTC."""
    return datetime.combine(day, dtime(0), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def split_by_day(start: datetime, end: datetime, tz) -> list[tuple[date, datetime, datetime]]:
    """`(local day, segment start, segment end)` pieces of `start..end` cut at local midnight."""
    pieces = []
    cursor = start
    while cursor < end:
        day = local_day(cursor, tz)
        piece_end = min(end, local_midnight(day + timedelta(days=1), tz))
        pieces.append((day, cursor, piece_end))
        cursor = piece_end
    return pieces


def _watermark(session: Session, model: str, column: str) -> datetime | None:
    """How far `model`'s metric stored in `column` is counted, or None if it never was."""
    stmt = select(func.max(getattr(DailyStat, column))).where(DailyStat.model == model, DailyStat.source == "lumen")
    return session.execute(stmt).scalar_one_or_none()


class _Rows:
    """The model's lumen rows touched by this run; final rows are never returned."""

    def __init__(self, session: Session, model: str):
        self.session = session
        self.model = model
        self.rows: dict[date, DailyStat | None] = {}

    def get(self, day: date) -> DailyStat | None:
        if day not in self.rows:
            row = get_daily(self.session, day, self.model, "lumen")
            if row is None:
                row = upsert_daily(self.session, day, self.model, "lumen")
            self.rows[day] = None if row.final else row
        return self.rows[day]


def _add_model(session: Session, model: dict, end: datetime, tz) -> None:
    model_id = model["id"]
    rows = _Rows(session, model_id)
    for metric, column in METRICS.items():
        covered = _watermark(session, model_id, column) or local_midnight(local_day(end, tz), tz)
        if end <= covered:
            continue
        span = plan_span(model[metric], covered, end)
        if span is None:
            continue
        for day, piece_start, piece_end in split_by_day(span.start, end, tz):
            row = rows.get(day)
            if row is None:
                continue
            seconds = (piece_end - piece_start).total_seconds()
            setattr(row, metric, (getattr(row, metric) or 0) + span.rate * seconds)
            row.estimated = bool(row.estimated) or span.estimated
            setattr(row, column, max(getattr(row, column) or piece_end, piece_end))
            row.last_updated = utcnow()


def _finalize(session: Session, end: datetime, tz) -> int:
    """Close earlier days that are fully counted or out of reach of every Lumen window."""
    stmt = select(DailyStat).where(DailyStat.source == "lumen", DailyStat.day < local_day(end, tz), DailyStat.final.is_(False))
    watermarks: dict[str, list[datetime]] = {}
    closed = 0
    for row in session.execute(stmt).scalars().all():
        if row.model not in watermarks:
            found = (_watermark(session, row.model, column) for column in METRICS.values())
            watermarks[row.model] = [w for w in found if w is not None]
        day_end = local_midnight(row.day + timedelta(days=1), tz)
        if any(w < day_end for w in watermarks[row.model]) and end - day_end < MONTH:
            continue
        row.final = True
        row.last_updated = utcnow()
        closed += 1
    return closed


def run_models(store: ConfigStore, session_factory: sessionmaker, snapshot: LiveSnapshot, now: datetime | None = None) -> TotalsResult:
    """Add the traffic since the last run to today's totals; see the module docstring."""
    config = store.current
    now = now or utcnow()
    snap = snapshot.get()
    if snap is None:
        log.info("models run skipped: no successful probe yet")
        return TotalsResult(skipped="no snapshot")
    if now - snap.polled_at > 2 * config.refresh.web:
        log.warning("models run skipped: the last successful probe is from %s", snap.polled_at)
        return TotalsResult(skipped="stale snapshot")

    end = snap.polled_at
    with session_factory() as session:
        for model in snap.models:
            _add_model(session, model, end, config.timezone)
        finalized = _finalize(session, end, config.timezone)
        session.commit()
    if finalized:
        log.info("models run closed %d daily rows", finalized)
    return TotalsResult(models=len(snap.models), finalized=finalized)
