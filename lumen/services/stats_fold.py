"""Fold ``request_logs`` into the ``model_stats`` / ``entity_stats`` totals.

After the cutover recorded in ``stats_fold_state``, billing stops bumping the
two stats tables on every request and marks the row it writes
``stats_deferred``; ``fold_once`` adds up the marked ``request_logs`` rows
written since its last pass instead. The cutover is set by the
``stats_deferred_marker`` migration on both dialects. Each pass covers the
half-open window ``[folded_until, now - lag)``, so consecutive windows never
overlap and never leave a gap; the lag leaves room for billing transactions
that commit a little after their row's ``time``.

A daemon thread started by ``create_app`` runs one pass every
``stats.fold_interval`` seconds. Until the cutover every pass is a no-op.

The thread runs with the other shared background workers (behind the
``BACKGROUND_WORKER`` switch), not with the per-process metrics snapshot: the
result lands in the database, and the compare-and-set on the watermark already
makes a second replica's pass a no-op.
"""

import logging
import math
import random
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import OperationalError

from lumen.extensions import db
from lumen.models.entity_stat import EntityStat
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services.llm import _bump
from lumen.services.stats_cutover import get_fold_state
from lumen.timeutils import as_utc_aware, as_utc_naive, utcnow

logger = logging.getLogger(__name__)

DEFAULT_FOLD_INTERVAL = 60  # seconds
DEFAULT_FOLD_LAG = 300  # seconds

#: The widest window one pass folds. After a long stall (the app stopped, or the
#: database unreachable for a while) the thread catches up in passes of at most
#: this size, back to back, rather than one transaction over the whole gap.
MAX_FOLD_WINDOW = timedelta(days=1)

#: How long the fold waits on a SQLite write lock held by a request before it
#: gives up and leaves the window to the next pass.
_SQLITE_BUSY_TIMEOUT_MS = 5000

#: Fraction of the interval to jitter each sleep by, so replicas do not align.
_JITTER = 0.1


def _seconds(app, key: str, default: float) -> float:
    """A positive, finite number of seconds from ``app.config``, else ``default``.

    ``apply_hot_config`` already filters the yaml; this keeps the thread alive
    whatever ends up in ``app.config``: an infinite interval would make
    ``Event.wait`` raise outside the thread's error handler and end it.
    """
    try:
        value = float(app.config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def fold_lag(app) -> timedelta:
    """How far behind ``now`` the fold window ends (``stats.fold_lag``)."""
    return timedelta(seconds=_seconds(app, "STATS_FOLD_LAG", DEFAULT_FOLD_LAG))


def fold_interval(app) -> float:
    """Seconds between passes (``stats.fold_interval``)."""
    return _seconds(app, "STATS_FOLD_INTERVAL", DEFAULT_FOLD_INTERVAL)


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
    watermark. A window wider than ``MAX_FOLD_WINDOW`` is cut to that size, and
    the rest is left to the next pass.

    The watermark advances first, by compare-and-set on the value just read:
    if another worker moved it in between, the UPDATE matches nothing and this
    pass rolls back without touching the stats tables, so no row is ever
    folded twice. On PostgreSQL a second concurrent pass blocks on that row
    until the first commits, then finds it moved.

    Only rows billing marked ``stats_deferred`` are folded; any other row in the
    window was bumped by billing already. Rows with a NULL ``entity_id`` have no
    stats row to land in and are skipped; rows with a NULL ``model_config_id``
    count for the entity only.

    Runs in its own app context, so on its own session and connection: a
    caller's session is neither committed nor rolled back by it.
    """
    with app.app_context():
        cutover_at, lo = get_fold_state()
        if cutover_at is None or lo is None:
            return None
        hi = min(as_utc_naive(now or utcnow()) - fold_lag(app), lo + MAX_FOLD_WINDOW)
        if hi <= lo:
            return None

        # On SQLite the compare-and-set is the statement that waits for a
        # request's write lock: it waits this long, then raises "database is
        # locked". The previous value is put back on the same connection
        # straight after, so the setting never returns to the pool with it.
        previous_busy_timeout = None
        if db.engine.dialect.name == "sqlite":
            previous_busy_timeout = db.session.execute(text("PRAGMA busy_timeout")).scalar_one()
            db.session.execute(text(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}"))
        try:
            moved = db.session.execute(
                sa_update(StatsFoldState)
                .where(StatsFoldState.id == 1, StatsFoldState.folded_until == lo)
                .values(folded_until=hi)
            )
        finally:
            if previous_busy_timeout is not None:
                db.session.execute(text(f"PRAGMA busy_timeout = {int(previous_busy_timeout)}"))
        if not moved.rowcount:
            db.session.rollback()
            return None

        in_window = (
            RequestLog.time >= as_utc_aware(lo),
            RequestLog.time < as_utc_aware(hi),
            RequestLog.entity_id.is_not(None),
            # Only the rows billing left to the fold: a NULL row was bumped
            # already (before the cutover, by an older pod, or on a stale
            # cached cutover_at).
            RequestLog.stats_deferred.is_(True),
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


def _is_sqlite_locked(exc: OperationalError) -> bool:
    return "database is locked" in str(exc.orig)


def run_fold_pass(app) -> Optional[datetime]:
    """One pass for the thread: ``fold_once``, with a locked SQLite database as a skip.

    A request holding the SQLite write lock past the busy timeout is normal
    load, not a failure: the pass rolls back whole (its own app context ends
    the session), the watermark has not moved, and the next pass folds the
    same window. Any other error propagates.
    """
    try:
        return fold_once(app)
    except OperationalError as exc:
        if not _is_sqlite_locked(exc):
            raise
        logger.info("stats fold: database is locked, retrying on the next pass")
        return None


def _sleep(seconds: float, stop: threading.Event) -> None:
    """Park between passes, waking early when the thread is asked to stop."""
    stop.wait(seconds)


#: Every fold thread started in this process, as (stop event, thread).
_folders: list[tuple[threading.Event, threading.Thread]] = []


def start_fold_thread(app):
    """Start the daemon thread that folds ``request_logs`` into the stats tables.

    Reads ``stats.fold_interval`` and ``stats.fold_lag`` from ``app.config`` on
    every pass, so a config reload applies to the next one.
    """
    stop = threading.Event()

    def run():
        while not stop.is_set():
            interval = fold_interval(app)
            behind = False
            try:
                hi = run_fold_pass(app)
                # Still more than one interval behind (catching up after a
                # stall, MAX_FOLD_WINDOW at a time): go again without sleeping.
                behind = hi is not None and utcnow() - fold_lag(app) - hi > timedelta(seconds=interval)
            except Exception:
                logger.exception("stats fold error")
            if not behind:
                _sleep(interval * (1 + random.uniform(-_JITTER, _JITTER)), stop)

    t = threading.Thread(target=run, daemon=True, name="stats-fold")
    _folders.append((stop, t))
    t.start()
    return t


def stop_fold_thread(timeout: float = 5.0) -> None:
    """Stop every fold thread started in this process and wait for it to finish."""
    folders, _folders[:] = list(_folders), []
    for stop, _ in folders:
        stop.set()
    for _, thread in folders:
        thread.join(timeout)
