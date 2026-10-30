"""Reading ``stats_fold_state``: the cutover instant and the fold watermark.

Its own module because both sides of the cutover need it: billing
(``lumen.services.llm``) stops bumping the stats tables from ``cutover_at``
on, and the fold (``lumen.services.stats_fold``, which imports ``llm``) starts
there.
"""
import threading
import time
from datetime import datetime
from typing import NamedTuple, Optional

from sqlalchemy import select, update

from lumen.extensions import db
from lumen.models.stats_fold_state import StatsFoldState

#: Seconds billing trusts its cached ``cutover_at``. This only cuts read load:
#: a process still holding a stale ``None`` bumps the stats tables and leaves its
#: row unmarked (``request_logs.stats_deferred`` NULL), and the fold skips
#: unmarked rows, so a stale value never counts a request twice.
CUTOVER_CACHE_TTL = 60.0


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


def lock_fold_state() -> FoldState:
    """``get_fold_state()``, holding the row until the transaction ends.

    For commands that must not interleave with a fold pass (``reassign-model``,
    ``check-stats``): the pass's compare-and-set on ``folded_until`` waits
    until the caller commits or rolls back. On PostgreSQL this is
    ``SELECT … FOR UPDATE``. SQLite has no row locks and ignores FOR UPDATE, so
    there a no-op UPDATE of the row takes the database write lock up front;
    call this before any other statement of the transaction, so the
    transaction never has to upgrade a read to a write behind a fold.
    """
    if db.engine.dialect.name == "sqlite":
        db.session.execute(
            update(StatsFoldState).where(StatsFoldState.id == 1)
            .values(folded_until=StatsFoldState.folded_until)
        )
    row = db.session.execute(
        select(StatsFoldState.cutover_at, StatsFoldState.folded_until)
        .where(StatsFoldState.id == 1).with_for_update()
    ).one_or_none()
    return FoldState(*row) if row else FoldState(None, None)


#: (monotonic expiry, cutover_at) of the last read, or None before the first.
_cached: Optional[tuple[float, Optional[datetime]]] = None
_lock = threading.Lock()


def cached_cutover_at() -> Optional[datetime]:
    """``cutover_at`` (naive UTC, None before the cutover), read at most once per TTL.

    Billing calls this on every request, so the row is read once a minute per
    process rather than once per request. Call inside an app context.
    """
    global _cached
    with _lock:
        cached = _cached
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    cutover_at = get_fold_state().cutover_at
    with _lock:
        _cached = (time.monotonic() + CUTOVER_CACHE_TTL, cutover_at)
    return cutover_at


def clear_cutover_cache() -> None:
    """Forget the cached ``cutover_at``, so the next billing call reads the row."""
    global _cached
    with _lock:
        _cached = None
