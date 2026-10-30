"""Fold ``request_logs`` into the ``model_stats`` / ``entity_stats`` totals.

After the cutover recorded in ``stats_fold_state``, billing stops bumping the
two stats tables on every request, and ``fold_once`` adds up the
``request_logs`` rows written since its last pass instead. Each pass covers the
half-open window ``[folded_until, now - lag)``, so consecutive windows never
overlap and never leave a gap; the lag leaves room for billing transactions
that commit a little after their row's ``time``.

Not wired into the app yet: nothing calls ``fold_once`` outside the tests.
"""

from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from lumen.extensions import db
from lumen.models.entity_stat import EntityStat
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services.llm import _bump
from lumen.timeutils import as_utc_aware, as_utc_naive, utcnow

DEFAULT_FOLD_LAG = 300  # seconds


class FoldState(NamedTuple):
    cutover_at: Optional[datetime]
    folded_until: Optional[datetime]


def get_fold_state() -> FoldState:
    """Return the cutover instant and the watermark, both naive UTC.

    The single reader of ``stats_fold_state``. A missing row (a database built
    with ``create_all()``) reads as ``FoldState(None, None)``: not cut over.
    Call inside an app context.
    """
    row = db.session.execute(
        select(StatsFoldState.cutover_at, StatsFoldState.folded_until).where(StatsFoldState.id == 1)
    ).one_or_none()
    return FoldState(*row) if row else FoldState(None, None)


def fold_lag(app) -> timedelta:
    """How far behind ``now`` the fold window ends."""
    return timedelta(seconds=app.config.get("STATS_FOLD_LAG", DEFAULT_FOLD_LAG))


def _sums():
    return (
        func.count().label("requests"),
        func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(RequestLog.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(RequestLog.audio_seconds), 0).label("audio_seconds"),
        func.coalesce(func.sum(RequestLog.cost), 0).label("cost"),
        func.max(RequestLog.time).label("last_used_at"),
    )


def _increments(model, row) -> tuple[dict, dict]:
    increments = {
        "requests": model.requests + row.requests,
        "input_tokens": model.input_tokens + row.input_tokens,
        "output_tokens": model.output_tokens + row.output_tokens,
        "audio_seconds": model.audio_seconds + row.audio_seconds,
        "cost": model.cost + row.cost,
    }
    insert_values = {
        "requests": row.requests, "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "audio_seconds": row.audio_seconds, "cost": row.cost,
    }
    return increments, insert_values


def fold_once(app, now: datetime = None) -> Optional[datetime]:
    """Fold one window of ``request_logs`` into the stats tables, in one transaction.

    Does nothing (returns None) before the cutover, or while the window
    ``[folded_until, now - lag)`` is empty. Otherwise returns the new
    watermark.

    The watermark advances first, by compare-and-set on the value just read:
    if another worker moved it in between, the UPDATE matches nothing and this
    pass rolls back without touching the stats tables, so no row is ever
    folded twice. On PostgreSQL a second concurrent pass blocks on that row
    until the first commits, then finds it moved.

    Rows with a NULL ``entity_id`` have no stats row to land in and are
    skipped; rows with a NULL ``model_config_id`` count for the entity only.
    """
    with app.app_context():
        lo = get_fold_state().folded_until
        if lo is None:
            return None
        hi = as_utc_naive(now or utcnow()) - fold_lag(app)
        if hi <= lo:
            return None

        moved = db.session.execute(
            sa_update(StatsFoldState)
            .where(StatsFoldState.id == 1, StatsFoldState.folded_until == lo)
            .values(folded_until=hi)
        )
        if not moved.rowcount:
            db.session.rollback()
            return None

        in_window = (
            RequestLog.time >= as_utc_aware(lo),
            RequestLog.time < as_utc_aware(hi),
            RequestLog.entity_id.is_not(None),
        )
        per_model = db.session.execute(
            select(RequestLog.entity_id, RequestLog.model_config_id, RequestLog.source, *_sums())
            .where(*in_window, RequestLog.model_config_id.is_not(None))
            .group_by(RequestLog.entity_id, RequestLog.model_config_id, RequestLog.source)
            .order_by(RequestLog.entity_id, RequestLog.model_config_id, RequestLog.source)
        ).all()
        per_entity = db.session.execute(
            select(RequestLog.entity_id, *_sums())
            .where(*in_window)
            .group_by(RequestLog.entity_id)
            .order_by(RequestLog.entity_id)
        ).all()

        # Same table order as billing (model_stats, then entity_stats).
        for row in per_model:
            increments, insert_values = _increments(ModelStat, row)
            _bump(
                ModelStat,
                {"entity_id": row.entity_id, "model_config_id": row.model_config_id, "source": row.source},
                increments, insert_values, last_used_at=as_utc_naive(row.last_used_at),
            )
        for row in per_entity:
            increments, insert_values = _increments(EntityStat, row)
            _bump(
                EntityStat, {"entity_id": row.entity_id},
                increments, insert_values, last_used_at=as_utc_naive(row.last_used_at),
            )
        db.session.commit()
        return hi
