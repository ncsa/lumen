"""Connection-pool checkout tracking, for attributing leaked DB connections.

The ``lumen_db_pool_connections`` gauges show *how many* connections are checked
out, not *who* holds them. A leak — a connection checked out and never returned,
so the pool creates a replacement and its ``checked_out`` count climbs and never
falls back — is invisible in the metrics beyond the climb itself.

This module records the endpoint, thread and stack of every checkout and drops
the record on check-in, so whatever is still outstanding after minutes is the
leak, named by call site. It is always on: a checkout happens a handful of times
per request and capturing a bounded stack costs microseconds against LLM calls
measured in seconds.

Exposed through ``/metrics/debug`` (see the metrics blueprint) and logged
automatically by :func:`watchdog` when the pool sits near capacity.
"""

import gc
import logging
import sys
import threading
import time
import traceback
import types
import weakref
from typing import NamedTuple

from flask import has_request_context, request
from sqlalchemy import event
from sqlalchemy.pool import Pool

logger = logging.getLogger(__name__)

# Stack frames captured per checkout: deep enough to cross the SQLAlchemy and
# Flask-SQLAlchemy plumbing and reach the application frame that triggered it.
_STACK_DEPTH = 25
# A checkout held longer than this is reported as stranded — no legitimate call
# site holds a connection across an LLM call (they all release it first).
STRANDED_AFTER = 300.0
# Consecutive near-capacity scrapes before the watchdog dumps stacks.
_PRESSURE_SCRAPES = 3
# Reference-graph hops walked when naming what retains a leaked connection, and
# the cap on names reported per object. Both bounded: the walk runs inside a
# request and gc.get_referrers is not cheap. Five hops is what it takes to reach
# the Session behind a checked-out connection (fairy -> Connection ->
# RootTransaction -> SessionTransaction -> Session); a sixth adds nothing.
_HOLDER_DEPTH = 5
_MAX_RETAINERS = 12
# Objects carried into the next hop of the reference walk. Caps the breadth
# explosion that follows once the walk reaches a module or class dict.
_MAX_WALK = 300

_lock = threading.Lock()
_outstanding: dict = {}
_registered = False
_pressure_count = 0


class Checkout(NamedTuple):
    """One outstanding pool checkout."""

    endpoint: str
    thread: str
    at: float  # time.monotonic() when the connection was checked out
    stack: str
    record_ref: weakref.ref  # to the SQLAlchemy _ConnectionRecord this describes

    def age(self, now: float = None) -> float:
        return (now if now is not None else time.monotonic()) - self.at

    def is_stale(self) -> bool:
        """True when the connection record is gone but the entry survived.

        Entries are keyed by ``id(connection_record)`` and pruned only by the
        check-in event, so a record collected without one leaves an entry that
        ages forever and reads as a leak. A dead weakref proves the connection
        is not actually held: the leak is in this bookkeeping, not the pool.
        """
        return self.record_ref() is None


def _on_checkout(dbapi_connection, connection_record, connection_proxy):
    endpoint = "no-request-context"
    if has_request_context():
        endpoint = request.endpoint or request.path
    record = Checkout(
        endpoint=endpoint,
        thread=threading.current_thread().name,
        at=time.monotonic(),
        stack="".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1]),
        record_ref=weakref.ref(connection_record),
    )
    with _lock:
        _outstanding[id(connection_record)] = record


def _on_checkin(dbapi_connection, connection_record):
    with _lock:
        _outstanding.pop(id(connection_record), None)


def init_pool_tracking():
    """Register the checkout/check-in listeners. Idempotent.

    Listens on the ``Pool`` class rather than a specific engine so no app context
    (and no engine creation) is needed at registration time.
    """
    global _registered
    if _registered:
        return
    event.listen(Pool, "checkout", _on_checkout)
    event.listen(Pool, "checkin", _on_checkin)
    _registered = True


def outstanding(min_age: float = 0.0) -> list:
    """Outstanding checkouts held at least ``min_age`` seconds, oldest first."""
    now = time.monotonic()
    with _lock:
        records = list(_outstanding.values())
    return sorted((r for r in records if r.age(now) >= min_age), key=lambda r: r.at)


def stranded_count() -> int:
    """Number of checkouts held long enough to be considered leaked.

    Stale entries are excluded: they hold no connection, so counting them would
    peg the ``stranded`` gauge and re-arm the watchdog forever.
    """
    return len([r for r in outstanding(STRANDED_AFTER) if not r.is_stale()])


def format_outstanding(min_age: float = 0.0, limit: int = 25) -> str:
    records = outstanding(min_age)
    if not records:
        return "(none)\n"
    now = time.monotonic()
    lines = [f"{len(records)} outstanding checkout(s), oldest first"]
    if len(records) > limit:
        lines.append(f"(showing the {limit} oldest)")
    for r in records[:limit]:
        stale = "  STALE-TRACKER-ENTRY (connection not actually held)" if r.is_stale() else ""
        lines.append(
            f"\n--- held {r.age(now):.0f}s  endpoint={r.endpoint}  thread={r.thread}{stale} ---\n{r.stack}"
        )
    return "\n".join(lines) + "\n"


def _describe(obj) -> str:
    """Identify a referring object by type only.

    Never reprs the object. A Session's identity map holds API key hashes and
    user rows, and this output is served over HTTP; the type name (or, for a
    frame, its code location) is what identifies a retainer anyway.
    """
    if isinstance(obj, types.FrameType):
        return f"frame {obj.f_code.co_filename}:{obj.f_lineno} in {obj.f_code.co_name}"
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _is_informative(name: str) -> bool:
    """Filter the reference graph's connective tissue.

    Every object is reachable from functions, modules, cells and iterators; those
    names identify nothing. Generators are kept — an abandoned one is a plausible
    retainer — and frames are described separately.
    """
    return not name.startswith("builtins.") or name == "builtins.generator"


def _retainers(obj, depth: int = _HOLDER_DEPTH, limit: int = _MAX_RETAINERS) -> list:
    """Per-hop type names of the objects transitively referencing ``obj``.

    One entry per hop, nearest first, so the chain ending at the retainer is
    visible rather than just its first link: a leaked ORM session reads as
    ``Connection`` / ``RootTransaction`` / ``SessionTransaction`` / ``Session``.
    Bounded in depth and breadth — ``gc.get_referrers`` on a module dict can
    return thousands of objects, and this runs inside a request.
    """
    seen = {id(obj)}
    frontier = [obj]
    hops = []
    for _ in range(depth):
        nxt: list = []
        names: list = []
        # This frame's own locals reference the objects being walked; skipping
        # them by identity keeps the probe out of its own results.
        skip = {id(seen), id(frontier), id(hops), id(nxt), id(names), id(sys._getframe())}
        for current in frontier:
            for ref in gc.get_referrers(current):
                if id(ref) in seen or id(ref) in skip:
                    continue
                seen.add(id(ref))
                if len(nxt) < _MAX_WALK:
                    nxt.append(ref)
                name = _describe(ref)
                if name not in names and _is_informative(name) and len(names) < limit:
                    names.append(name)
        if names:
            hops.append(", ".join(names))
        if not nxt:
            break
        frontier = nxt
    return hops


def format_holders(min_age: float = STRANDED_AFTER, limit: int = 5) -> str:
    """What still holds each long-held connection.

    The checkout stack names where a connection was *acquired*, which need not be
    where it is *retained*: the acquiring thread can be back in the worker pool
    while the connection stays out. Confirmed in production — a checkout attributed
    to ``api.list_models`` aged past an hour while its thread sat idle. This walks
    from the pool's own record to whatever references it, which is the only way to
    name the retainer, and reports whether the DBAPI connection is already dead
    (Postgres reaps an orphaned backend at ``idle_in_transaction_session_timeout``,
    so a leak older than that leaves a live pool record over a dead socket).

    Touches SQLAlchemy pool internals (``fairy_ref``, ``dbapi_connection``) because
    no public API exposes the holder of a checked-out connection.
    """
    records = outstanding(min_age)
    if not records:
        return "(none)\n"
    lines = []
    for r in records[:limit]:
        lines.append(f"\n--- endpoint={r.endpoint}  thread={r.thread}  held {r.age():.0f}s ---")
        rec = r.record_ref()
        if rec is None:
            lines.append("  connection record collected — stale tracker entry, no connection held")
            continue
        dbapi = getattr(rec, "dbapi_connection", None)
        # psycopg2/psycopg expose .closed (0 = open); other drivers may not.
        closed = getattr(dbapi, "closed", "unknown") if dbapi is not None else "no connection"
        lines.append(f"  dbapi_connection: {'present' if dbapi is not None else 'None'}  closed={closed}")
        fairy_ref = getattr(rec, "fairy_ref", None)
        fairy = fairy_ref() if fairy_ref is not None else None
        lines.append(f"  fairy: {'live' if fairy is not None else 'gone'}")
        # Only the fairy is walked: the record is referenced by pool internals no
        # matter who leaked it, so its retainers name nothing.
        if fairy is None:
            lines.append("  retainers: fairy gone — nothing holds this checkout in Python")
        else:
            hops = _retainers(fairy)
            for i, hop in enumerate(hops, start=1):
                lines.append(f"  retainers hop {i}: {hop}")
            if not hops:
                lines.append("  retainers: (none found)")
    return "\n".join(lines) + "\n"


def thread_dump() -> str:
    """Stacks of every live thread — identifies a worker wedged mid-request."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        parts.append(f"\n--- thread {names.get(ident, 'unknown')} ({ident}) ---\n")
        parts.extend(traceback.format_stack(frame))
    return "".join(parts)


def watchdog(checked_out: float, limit: float):
    """Log outstanding-checkout stacks once the pool stays near capacity.

    Called from the metrics collector on every scrape, so "consecutive scrapes"
    is measured in scrape intervals. Logs once per episode (not on every scrape)
    to keep an exhausted pool from flooding the log, and resets when the pool
    recovers.
    """
    global _pressure_count
    if not limit or checked_out < 0.9 * limit:
        _pressure_count = 0
        return
    _pressure_count += 1
    if _pressure_count != _PRESSURE_SCRAPES:
        return
    logger.error(
        "DB pool at %d/%d checked out for %d consecutive scrapes; %d checkout(s) "
        "held over %.0fs. Outstanding checkouts:\n%s",
        checked_out, limit, _PRESSURE_SCRAPES, stranded_count(), STRANDED_AFTER,
        format_outstanding(),
    )
