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
"""
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
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
        while not stop.is_set():
            try:
                # The app context is entered here and exited before the sleep
                # below, deliberately: hoisting it out of the loop ("why push it
                # 1440 times a day?") holds a session across the sleep and
                # reproduces the 1.22.0 idle-in-transaction leak, one leaked
                # connection per process. A sleep is a yield in every way that
                # matters — no session and no Flask context may outlive the work
                # that needs it.
                with app.app_context():
                    refresh_snapshot()
            except Exception:
                # Without this the thread dies and the snapshot ages forever
                # with nothing logged after the first traceback.
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
