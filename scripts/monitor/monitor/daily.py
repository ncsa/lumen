"""Daily rows: the local day and the latest-of-day upsert into `daily_stats`."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from monitor.db import utcnow
from monitor.models import DailyStat


def local_day(now: datetime, tz: ZoneInfo) -> date:
    """The local date in `tz` of the naive UTC time `now`."""
    return now.replace(tzinfo=timezone.utc).astimezone(tz).date()


def get_daily(session: Session, day: date, model: str, source: str) -> DailyStat | None:
    stmt = select(DailyStat).where(DailyStat.day == day, DailyStat.model == model, DailyStat.source == source)
    return session.execute(stmt).scalar_one_or_none()


def upsert_daily(session: Session, day: date, model: str, source: str, **values) -> DailyStat:
    """Set `values` on the `(day, model, source)` row, creating it if needed.

    Columns not in `values` keep what other collectors wrote. The caller commits.
    """
    row = get_daily(session, day, model, source)
    if row is None:
        row = DailyStat(day=day, model=model, source=source)
        session.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    row.last_updated = utcnow()
    return row
