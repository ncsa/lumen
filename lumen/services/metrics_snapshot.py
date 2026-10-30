"""Background-refreshed snapshot of the database-derived metrics.

``LumenDBCollector`` used to run its three statements on every Prometheus
scrape, taking a pooled connection to do it — so ``/metrics`` competed for the
pool it was reporting on and could block up to ``pool_timeout`` during exactly
the burst it exists to describe. A daemon thread refreshes an in-memory
snapshot instead and ``collect()`` is a pure read of it.

**Never elect this refresher.** ``lumen/services/health.py`` runs a flock
election so only one process probes, and the same pattern here would be a
fleet-breaking bug: the non-holders would hold an empty snapshot forever, and a
counter that disappears and reappears reads as a reset, so ``rate()`` invents a
spike on every recovery. The general rule: **elect only work whose result lands
in the DB.** Health probing qualifies (the holder writes ``ModelEndpoint.healthy``
and every non-holder reads the same row back); an in-memory object only the
refreshing process can see does not. Never elect in-memory state.

For the same reason the refresher starts outside the ``BACKGROUND_WORKER``
guard in ``create_app`` — that switch keeps extra workers from duplicating
*shared* work, and turning it on must not leave a worker with an empty snapshot.

The same thread also keeps the TimescaleDB health (#173) on its own, slower
cadence; see :class:`MetricsSnapshot` for why it is not part of the snapshot.
"""
import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.services import timescale_health
from lumen.services.timescale_health import TimescaleHealth
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)


def _sleep(seconds: float, stop: threading.Event) -> None:
    """Park between passes, waking early if the refresher has been asked to stop.

    Waiting on an event rather than sleeping blindly is the thread's only
    shutdown path: a ``time.sleep(60)`` is uninterruptible, so the thread is
    still mid-loop when the interpreter tears down and anything raised at that
    point surfaces as an unhandled exception in a thread nobody can stop.

    It is also the indirection tests patch to park the refresher mid-loop and
    assert that it holds neither a pooled connection nor an app context while it
    sleeps.
    """
    stop.wait(seconds)


#: Every refresher started in this process, as (stop event, thread). ``create_app``
#: can run more than once per process (the test suite does), so each thread owns
#: its own event and :func:`stop_snapshot_refresher` stops all of them.
_refreshers: list[tuple[threading.Event, threading.Thread]] = []

#: Never refresh faster than this, whatever the topology says.
_MIN_REFRESH_INTERVAL = 30.0

#: Refresh interval when the sizing rule below asks for less.
_DEFAULT_REFRESH_INTERVAL = 60.0

#: Prometheus scrape interval this deployment is assumed to use — the chart's
#: ``serviceMonitor.interval`` default. The refresh cost is
#: ``processes x replicas x 3 statements / interval``, so a per-process refresh
#: faster than ``processes x scrape_interval`` would cost the database *more*
#: than the per-scrape queries it replaces.
_ASSUMED_SCRAPE_INTERVAL = 30.0

#: Fraction of the interval to jitter each sleep by, so the N workers of a pod
#: do not align their passes into one burst against the pool.
_JITTER = 0.1

#: Seconds between TimescaleDB health checks, rounded up to whole passes. Well
#: inside the 3-hour ``start_offset`` of ``request_counts_hourly``, so a failing
#: refresh job shows on /metrics long before it can leave a gap.
_TIMESCALE_HEALTH_INTERVAL = 300.0

#: Problems that mean the usage charts cannot work at all; logged as ERROR, as at startup.
_FATAL_PROBLEMS = frozenset({"extension", "hypertable"})

_PROCESS_START = time.monotonic()


@dataclass(frozen=True)
class ModelUsage:
    """Cumulative usage for one (model, source) pair."""

    requests: int
    input_tokens: int
    output_tokens: int
    cost: float


@dataclass(frozen=True)
class MetricsSnapshot:
    """Everything ``LumenDBCollector`` reads, captured in one pass.

    Adding a field costs a statement on this pass, so each addition must state
    the exact statement it adds, whether that statement is index-backed, and
    that it rides *this* pass — never a per-model loop. A pass is capped at
    five statements.

    The TimescaleDB health is deliberately not a field here. Its check costs up
    to four catalog statements, which would take a pass past the cap, and it
    only needs refreshing every few minutes. It is kept beside the snapshot
    (:func:`get_timescale_health`), checked every ``_TIMESCALE_HEALTH_INTERVAL``
    seconds on the same thread, after the snapshot pass and in its own
    ``try``, so a failing check can neither delay nor stale the snapshot.
    """

    primed: bool
    captured_at: float  # time.monotonic(), the only input to the age gauge
    captured_wall: datetime  # naive UTC, for display only
    model_usage: dict[tuple[str, str], ModelUsage]
    endpoint_health: tuple[tuple[str, str, bool], ...]
    users_active: int
    users_total: int


#: Age is measured from process start while unprimed, because that gauge is
#: precisely the signal an operator alerts on for this state.
_UNPRIMED = MetricsSnapshot(
    primed=False,
    captured_at=_PROCESS_START,
    captured_wall=utcnow(),
    model_usage={},
    endpoint_health=(),
    users_active=0,
    users_total=0,
)

_snapshot = _UNPRIMED


def get_snapshot() -> MetricsSnapshot:
    """The most recent snapshot, or the unprimed one before the first pass.

    Never triggers a refresh. A refresh-if-stale branch here would put the
    database back on the scrape path on exactly the scrape that follows a slow
    period — the failure this module exists to remove.
    """
    return _snapshot


def refresh_snapshot() -> MetricsSnapshot:
    """Run one pass and publish the result. Caller owns the app context."""
    global _snapshot

    rows = db.session.execute(
        select(
            ModelConfig.model_name,
            ModelStat.source,
            func.coalesce(func.sum(ModelStat.requests), 0),
            func.coalesce(func.sum(ModelStat.input_tokens), 0),
            func.coalesce(func.sum(ModelStat.output_tokens), 0),
            func.coalesce(func.sum(ModelStat.cost), 0),
        )
        .join(ModelConfig, ModelStat.model_config_id == ModelConfig.id)
        .group_by(ModelConfig.model_name, ModelStat.source)
    ).all()
    model_usage = {
        (model_name, source): ModelUsage(int(reqs), int(inp), int(out), float(cost))
        for model_name, source, reqs, inp, out, cost in rows
    }

    endpoint_health = tuple(
        (model_name, url, bool(healthy))
        for model_name, url, healthy in db.session.execute(
            select(ModelConfig.model_name, ModelEndpoint.url, ModelEndpoint.healthy)
            .join(ModelConfig, ModelEndpoint.model_config_id == ModelConfig.id)
            .where(ModelEndpoint.active.is_(True))
        ).all()
    )

    # active = admin has not disabled the user (Entity.active=True, the default)
    # total  = all users ever registered
    users_active = db.session.scalar(
        select(func.count(Entity.id)).filter_by(entity_type="user", active=True)
    ) or 0
    users_total = db.session.scalar(
        select(func.count(Entity.id)).filter_by(entity_type="user")
    ) or 0

    _snapshot = MetricsSnapshot(
        primed=True,
        captured_at=time.monotonic(),
        captured_wall=utcnow(),
        model_usage=model_usage,
        endpoint_health=endpoint_health,
        users_active=int(users_active),
        users_total=int(users_total),
    )
    return _snapshot


@dataclass(frozen=True)
class TimescaleHealthSnapshot:
    """The latest TimescaleDB health check in this process."""

    health: TimescaleHealth
    captured_at: float  # time.monotonic(), the only input to the age gauge


#: ``None`` until the first check, and forever on SQLite.
_timescale: Optional[TimescaleHealthSnapshot] = None

#: The last health-check failure logged, so a persistent one is logged once.
_timescale_error: Optional[str] = None


def get_timescale_health() -> Optional[TimescaleHealthSnapshot]:
    """The latest TimescaleDB health check, or ``None`` before the first. Never queries."""
    return _timescale


def publish_timescale_health(health: TimescaleHealth) -> TimescaleHealthSnapshot:
    """Make *health* the current check, without logging it.

    The startup check publishes through here: it has just logged its own
    summary, so the refresher's first check only logs what changed since.
    """
    global _timescale
    _timescale = TimescaleHealthSnapshot(health=health, captured_at=time.monotonic())
    return _timescale


def refresh_timescale_health() -> Optional[TimescaleHealthSnapshot]:
    """Check TimescaleDB, log what changed and publish the result. Caller owns the app context.

    PostgreSQL only; on SQLite nothing is checked or published.
    """
    global _timescale_error
    if db.engine.dialect.name != "postgresql":
        return None
    health = timescale_health.check(db.session)
    _timescale_error = None
    _log_health_changes(_timescale.health if _timescale else None, health)
    return publish_timescale_health(health)


def _log_health_changes(previous: Optional[TimescaleHealth], current: TimescaleHealth) -> None:
    """Log a new problem, a cleared problem, or a job's ``total_failures`` rising.

    Every replica checks every few minutes, so a problem that persists is logged
    once per process, not on every check. Without a previous check (the startup
    check failed) every current problem is new.
    """
    now = timescale_health.keyed_problems(current)
    before = timescale_health.keyed_problems(previous) if previous else {}
    for key, message in now.items():
        if before.get(key) != message:
            level = logging.ERROR if key in _FATAL_PROBLEMS else logging.WARNING
            logger.log(level, "TimescaleDB: %s", message)
    for key, message in before.items():
        if key not in now:
            logger.info("TimescaleDB problem cleared: %s", message)
    if previous is None:
        return
    for name, job in current.jobs:
        old = previous.job(name)
        # A job that is failing now has just been logged with its new count.
        if old is None or f"job:{name}" in now:
            continue
        failures, old_failures = job.total_failures or 0, old.total_failures or 0
        if failures > old_failures:
            logger.warning(
                "TimescaleDB: background job %s (job %d) failed %d time(s) since the last "
                "check; %d failures in total.",
                name, job.job_id, failures - old_failures, failures,
            )


def _run_pass(check_timescale: bool) -> None:
    """One pass inside the caller's app context: the snapshot, then the health check if due.

    Each half has its own ``try``. The health check runs second, so it cannot
    delay the snapshot, and a raising check leaves the snapshot just published.
    """
    global _timescale_error
    try:
        refresh_snapshot()
    except Exception:
        # Without this the thread dies and the snapshot ages forever
        # with nothing logged after the first traceback.
        logger.exception("metrics snapshot refresh error")
        db.session.rollback()
    if not check_timescale:
        return
    try:
        refresh_timescale_health()
    except Exception as e:  # noqa: BLE001 - a health check must never stop the refresher
        db.session.rollback()
        if str(e) != _timescale_error:
            logger.warning("Could not check TimescaleDB health: %s", e)
        _timescale_error = str(e)


def refresh_interval(app) -> float:
    """Seconds between passes: ``processes x scrape_interval``, floor 30s.

    Per-process refreshing can *increase* total database load — the old cost was
    three statements per scrape per pod, the new one is three per process per
    interval — so the interval scales with the worker count rather than being a
    flat number.
    """
    workers = app.config.get("POOL_TOPOLOGY", {}).get("workers", 1) or 1
    interval = max(_DEFAULT_REFRESH_INTERVAL, workers * _ASSUMED_SCRAPE_INTERVAL)
    return max(_MIN_REFRESH_INTERVAL, interval)


def start_snapshot_refresher(app):
    """Start the daemon thread that refreshes the snapshot. Never elected."""
    stop = threading.Event()

    def run():
        passes = 0
        while not stop.is_set():
            health_every = math.ceil(_TIMESCALE_HEALTH_INTERVAL / refresh_interval(app))
            check_timescale = passes % health_every == 0
            passes += 1
            try:
                # The app context is entered here and exited before the sleep
                # below, deliberately: hoisting it out of the loop ("why push it
                # 1440 times a day?") holds a session across the sleep and
                # reproduces the 1.22.0 idle-in-transaction leak, one leaked
                # connection per process. A sleep is a yield in every way that
                # matters — no session and no Flask context may outlive the work
                # that needs it.
                with app.app_context():
                    _run_pass(check_timescale)
            except Exception:
                # Entering or leaving the context failed; the loop must survive it.
                logger.exception("metrics snapshot refresh error")
            interval = refresh_interval(app)
            _sleep(interval * (1 + random.uniform(-_JITTER, _JITTER)), stop)

    t = threading.Thread(target=run, daemon=True, name="metrics-snapshot")
    _refreshers.append((stop, t))
    t.start()
    return t


def stop_snapshot_refresher(timeout: float = 5.0) -> None:
    """Stop every refresher started in this process and wait for it to finish.

    Tests use this to end the thread deterministically instead of throwing into
    it from a patched ``_sleep``; a thread torn down by an exception is reported
    as an unhandled thread exception and makes the whole suite noisy.
    """
    refreshers, _refreshers[:] = list(_refreshers), []
    for stop, _ in refreshers:
        stop.set()
    for _, thread in refreshers:
        thread.join(timeout)
