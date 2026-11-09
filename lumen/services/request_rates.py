"""Per-model request counts over the last minute, hour and day, and token sums.

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
  Rows stamped after the current minute are in no window.
- ``last_month`` (token sums only) is 30 days, built the same way; it matches
  the aggregate's 30-day retention.
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
        "month_start": minute - timedelta(days=30),
    }


def rates(model_config_id: int | None = None) -> dict:
    """Return ``{model_config_id: {last_minute, last_hour, last_day}}``.

    Models with no request in the last day are absent. ``model_config_id``
    restricts the result to that one model.
    """
    params = _windows(_now())

    if db.engine.dialect.name == "postgresql":
        t, mid = _METRICS_1M.c.bucket, _METRICS_1M.c.model_config_id

        def count(*where):
            n = func.sum(_METRICS_1M.c.requests)
            return func.coalesce(n.filter(*where) if where else n, 0)
    else:
        t, mid = RequestLog.time, RequestLog.model_config_id

        def count(*where):
            return func.count().filter(*where) if where else func.count()

    stmt = (
        select(
            mid,
            count(t >= params["minute_start"], t < params["minute_end"]),
            count(t >= params["hour_start"]),
            count(),
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

    return {
        model_id: {"last_minute": int(m), "last_hour": int(h), "last_day": int(d)}
        for model_id, m, h, d in rows
    }


TOKEN_WINDOWS = ("last_minute", "last_hour", "last_day", "last_month")


def token_windows(model_config_id: int | None = None) -> dict:
    """Return ``{model_config_id: {input_tokens: {...}, output_tokens: {...}}}``.

    Each inner dict holds the token sum for ``last_minute`` / ``last_hour`` /
    ``last_day`` / ``last_month``. Models with no request in the last month are
    absent. ``model_config_id`` restricts the result to that one model.
    """
    params = _windows(_now())

    if db.engine.dialect.name == "postgresql":
        c = _METRICS_1M.c
        t, mid, inputs, outputs = c.bucket, c.model_config_id, c.input_tokens, c.output_tokens
    else:
        t, mid = RequestLog.time, RequestLog.model_config_id
        inputs, outputs = RequestLog.input_tokens, RequestLog.output_tokens

    # One filter per window, in TOKEN_WINDOWS order; last_month is the WHERE.
    edges = (
        (t >= params["minute_start"], t < params["minute_end"]),
        (t >= params["hour_start"],),
        (t >= params["day_start"],),
        (),
    )

    def total(col, *where):
        n = func.sum(col)
        return func.coalesce(n.filter(*where) if where else n, 0)

    stmt = (
        select(mid, *(total(col, *where) for col in (inputs, outputs) for where in edges))
        .where(t >= params["month_start"], t < params["next_minute"], mid.isnot(None))
        .group_by(mid)
    )
    if model_config_id is not None:
        stmt = stmt.where(mid == model_config_id)
    rows = db.session.execute(stmt).all()

    n = len(TOKEN_WINDOWS)
    return {
        model_id: {
            "input_tokens": dict(zip(TOKEN_WINDOWS, map(int, sums[:n]))),
            "output_tokens": dict(zip(TOKEN_WINDOWS, map(int, sums[n:]))),
        }
        for model_id, *sums in rows
    }
