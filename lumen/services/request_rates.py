"""Per-model request and token counts over the last minute, hour and day.

Shared by ``/v1/models`` and the model detail page. On PostgreSQL it reads the
real-time ``request_metrics_1m`` continuous aggregate, so neither caller scans
raw ``request_logs``; on SQLite (dev/test), where the aggregate does not exist,
it counts ``request_logs`` rows in one pass. The same query returns the request
count and the input and output token sums for every window.

The aggregate counts whole minutes, so every window is a set of whole minute
buckets and both backends use the same edges:

- ``last_minute`` is the last *complete* minute only, ``[minute - 1m, minute)``
  where ``minute`` is the start of the current minute. The current, partial
  minute is not in it.
- ``last_hour`` and ``last_day`` start at the minute bucket holding
  ``now - window`` and run up to and including the current partial minute.
  Rows stamped after the current minute are in no window.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import column, func, select, table

from lumen.extensions import db
from lumen.models.request_log import RequestLog

# The columns of the request_metrics_1m continuous aggregate this module reads
# (migration h2i3j4k5l6m7). It is a view, so it has no ORM model.
_METRICS_1M = table(
    "request_metrics_1m",
    column("bucket"),
    column("model_config_id"),
    column("requests"),
    column("input_tokens"),
    column("output_tokens"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _windows(now: datetime) -> dict:
    minute = now.replace(second=0, microsecond=0)
    return {
        "minute_start": minute - timedelta(minutes=1),
        "minute_end": minute,
        "next_minute": minute + timedelta(minutes=1),
        "hour_start": minute - timedelta(hours=1),
        "day_start": minute - timedelta(days=1),
    }


METRICS = ("requests", "input_tokens", "output_tokens")


def rates(model_config_id: int | None = None) -> dict:
    """Return ``{model_config_id: {metric: {last_minute, last_hour, last_day}}}``.

    ``metric`` is each of ``METRICS``: the request count and the input and
    output token sums, all over the same windows.

    Models with no request in the last day are absent. ``model_config_id``
    restricts the result to that one model.
    """
    params = _windows(_now())

    if db.engine.dialect.name == "postgresql":
        t, mid = _METRICS_1M.c.bucket, _METRICS_1M.c.model_config_id
        totals = {metric: func.sum(_METRICS_1M.c[metric]) for metric in METRICS}
    else:
        t, mid = RequestLog.time, RequestLog.model_config_id
        totals = {
            "requests": func.count(),
            "input_tokens": func.sum(RequestLog.input_tokens),
            "output_tokens": func.sum(RequestLog.output_tokens),
        }

    windows = (
        (t >= params["minute_start"], t < params["minute_end"]),
        (t >= params["hour_start"],),
        (),
    )
    stmt = (
        select(
            mid,
            *(
                func.coalesce(totals[metric].filter(*where) if where else totals[metric], 0)
                for metric in METRICS
                for where in windows
            ),
        )
        # The upper bound is the end of the current minute bucket: a row
        # stamped in a later minute (clock skew) is not counted until that
        # minute arrives, while one later in the current minute already is.
        .where(t >= params["day_start"], t < params["next_minute"], mid.isnot(None))
        .group_by(mid)
    )
    if model_config_id is not None:
        stmt = stmt.where(mid == model_config_id)
    rows = db.session.execute(stmt).all()

    result = {}
    for model_id, *values in rows:
        values = iter(values)
        result[model_id] = {
            metric: {name: int(next(values)) for name in ("last_minute", "last_hour", "last_day")}
            for metric in METRICS
        }
    return result
