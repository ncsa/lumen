"""Per-model request counts over the last minute, hour and day.

Shared by ``/v1/models`` and the model detail page. On PostgreSQL it reads the
real-time ``request_metrics_1m`` continuous aggregate, so neither caller scans
raw ``request_logs``; on SQLite (dev/test), where the aggregate does not exist,
it counts ``request_logs`` rows in one pass.

The aggregate counts whole minutes, so every window is a set of whole minute
buckets and both backends use the same edges:

- ``last_minute`` is the last *complete* minute only, ``[minute - 1m, minute)``
  where ``minute`` is the start of the current minute. The current, partial
  minute is not in it.
- ``last_hour`` and ``last_day`` start at the minute bucket holding
  ``now - window`` and run up to and including the current partial minute.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from lumen.extensions import db
from lumen.models.request_log import RequestLog

_RATES_SQL = """
    SELECT model_config_id,
           COALESCE(SUM(requests) FILTER (WHERE bucket >= :minute_start AND bucket < :minute_end), 0),
           COALESCE(SUM(requests) FILTER (WHERE bucket >= :hour_start), 0),
           COALESCE(SUM(requests), 0)
    FROM request_metrics_1m
    WHERE bucket >= :day_start AND model_config_id IS NOT NULL{model_filter}
    GROUP BY model_config_id
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _windows(now: datetime) -> dict:
    minute = now.replace(second=0, microsecond=0)
    return {
        "minute_start": minute - timedelta(minutes=1),
        "minute_end": minute,
        "hour_start": minute - timedelta(hours=1),
        "day_start": minute - timedelta(days=1),
    }


def rates(model_config_id: int | None = None) -> dict:
    """Return ``{model_config_id: {last_minute, last_hour, last_day}}``.

    Models with no request in the last day are absent. ``model_config_id``
    restricts the result to that one model.
    """
    params = _windows(_now())

    if db.engine.dialect.name == "postgresql":
        model_filter = ""
        if model_config_id is not None:
            model_filter = " AND model_config_id = :model_config_id"
            params["model_config_id"] = model_config_id
        rows = db.session.execute(text(_RATES_SQL.format(model_filter=model_filter)), params).all()
    else:
        t = RequestLog.time
        stmt = (
            select(
                RequestLog.model_config_id,
                func.count().filter(t >= params["minute_start"], t < params["minute_end"]),
                func.count().filter(t >= params["hour_start"]),
                func.count(),
            )
            .where(t >= params["day_start"], RequestLog.model_config_id.isnot(None))
            .group_by(RequestLog.model_config_id)
        )
        if model_config_id is not None:
            stmt = stmt.where(RequestLog.model_config_id == model_config_id)
        rows = db.session.execute(stmt).all()

    return {
        mid: {"last_minute": int(m), "last_hour": int(h), "last_day": int(d)}
        for mid, m, h, d in rows
    }
