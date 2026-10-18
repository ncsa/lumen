"""The models run: restart-safe daily request and token totals.

The run makes no HTTP call. It reads the latest successful probe snapshot
(skipped when it is older than 2 × `refresh.web`) and adds the traffic since
`covered_until` to each model's lumen daily row, then moves `covered_until` to
the snapshot time. With `gap = snapshot time − covered_until`:

- `gap ≤ 1h`: add `last_hour × gap/1h`.
- `gap ≤ 24h`: add `last_day × gap/24h` and mark the rows `estimated`.
- longer, with `last_month`: add `last_month × gap/30d` (at most 30 days back)
  and fill the missed days with `estimated` rows. Without `last_month`, go
  back at most 24h with `last_day` and leave the missed days empty.

A span that crosses local midnight is split by duration between the days.
`covered_until` lives in the database, so a restart picks up where the last
run stopped and never counts the same time twice. A model seen for the first
time is counted from local midnight. Requests and tokens are planned
separately, since Lumen may report a window for one and not the other.

After the totals, every lumen row of an earlier day is marked `final`; final
rows never change again, so the first run after midnight closes yesterday.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from sqlalchemy import func, select, update
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
METRICS = ("requests", "tokens")


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


def _covered_until(session: Session, model: str) -> datetime | None:
    stmt = select(func.max(DailyStat.covered_until)).where(DailyStat.model == model, DailyStat.source == "lumen")
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
    covered = _covered_until(session, model_id)
    if covered is None:
        covered = local_midnight(local_day(end, tz), tz)
    if end <= covered:
        return
    rows = _Rows(session, model_id)
    for metric in METRICS:
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
            row.covered_until = max(row.covered_until or piece_end, piece_end)
            row.last_updated = utcnow()
    today = rows.get(local_day(end, tz))
    if today is not None:
        today.covered_until = end


def _finalize(session: Session, today: date) -> int:
    stmt = (
        update(DailyStat)
        .where(DailyStat.source == "lumen", DailyStat.day < today, DailyStat.final.is_(False))
        .values(final=True, last_updated=utcnow())
    )
    return session.execute(stmt).rowcount or 0


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
        finalized = _finalize(session, local_day(end, config.timezone))
        session.commit()
    if finalized:
        log.info("models run closed %d daily rows", finalized)
    return TotalsResult(models=len(snap.models), finalized=finalized)
