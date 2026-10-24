"""The probe run: `GET /v1/models`, Lumen state and retention.

Every probe writes one `probes` row, success or not, with its response time.
A successful probe also writes one `samples` row per canonical model, updates
today's `polls` / `polls_ok` / `healthy_sum` counters on the model's lumen
daily row and replaces the in-memory live snapshot.

The Lumen state is read from the last probes in the database, so it survives
a restart. The first probe of a local day prunes `probes` and `samples` older
than `retention`; "first" is decided from the previous probe's day in the
database, so a restart does not prune twice.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from monitor import lumen_api
from monitor.config import ConfigStore
from monitor.daily import local_day, upsert_daily
from monitor.db import utcnow
from monitor.models import Probe, Sample

log = logging.getLogger(__name__)

UP = "UP"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
UNKNOWN = "UNKNOWN"
# How many of the latest probes decide DOWN.
STATE_WINDOW = 3


@dataclass(frozen=True)
class Snapshot:
    """The canonical models from the latest successful probe."""

    polled_at: datetime
    models: list[dict] = field(default_factory=list)


class LiveSnapshot:
    """Holds the latest `Snapshot`; shared between the scheduler and the web app."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def get(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def set(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot


@dataclass(frozen=True)
class ProbeResult:
    polled_at: datetime
    success: bool
    response_time_ms: float
    model_count: int | None = None
    models_up: int | None = None
    pruned: bool = False
    error: str | None = None


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _last_minute(windows: dict | None) -> int | None:
    return _int(windows.get("last_minute")) if windows else None


def lumen_state(session: Session) -> str:
    """UP, DEGRADED, DOWN or UNKNOWN from the last `STATE_WINDOW` probes.

    DOWN when every one of them failed, DEGRADED when the latest succeeded with
    fewer models up than listed, UNKNOWN only when there is no probe yet, UP
    otherwise (a single failure among successes is not an outage).
    """
    stmt = select(Probe).order_by(Probe.polled_at.desc(), Probe.id.desc()).limit(STATE_WINDOW)
    probes = session.execute(stmt).scalars().all()
    if not probes:
        return UNKNOWN
    if not any(p.success for p in probes):
        return DOWN
    latest = probes[0]
    if latest.success and latest.models_up is not None and latest.model_count is not None and latest.models_up < latest.model_count:
        return DEGRADED
    return UP


def _is_first_probe_of_day(session: Session, polled_at: datetime, tz) -> bool:
    previous = session.execute(select(Probe.polled_at).order_by(Probe.polled_at.desc()).limit(1)).scalar_one_or_none()
    return previous is None or local_day(previous, tz) != local_day(polled_at, tz)


def prune(session: Session, cutoff: datetime) -> None:
    """Delete probes and samples older than `cutoff`. The caller commits."""
    session.execute(delete(Probe).where(Probe.polled_at < cutoff))
    session.execute(delete(Sample).where(Sample.sampled_at < cutoff))


def _record_models(session: Session, polled_at: datetime, day, models: list[dict]) -> None:
    for model in models:
        status = model["status"] or "unknown"
        healthy = _int(model["healthy"])
        configured = _int(model["configured"])
        session.add(Sample(
            model=model["id"],
            sampled_at=polled_at,
            status=status,
            configured=configured,
            healthy=healthy,
            requests_last_minute=_last_minute(model["requests"]),
            input_tokens_last_minute=_last_minute(model.get("input_tokens")),
            output_tokens_last_minute=_last_minute(model.get("output_tokens")),
        ))
        row = upsert_daily(session, day, model["id"], "lumen", status=status, configured=configured, healthy=healthy)
        row.polls = (row.polls or 0) + 1
        row.polls_ok = (row.polls_ok or 0) + (status == "ok")
        row.healthy_sum = (row.healthy_sum or 0) + (healthy or 0)


def run_probe(
    store: ConfigStore,
    session_factory: sessionmaker,
    snapshot: LiveSnapshot | None = None,
    now: datetime | None = None,
    clock=time.monotonic,
) -> ProbeResult:
    """Probe Lumen once and record the result; never raises for a Lumen failure."""
    config = store.current
    polled_at = now or utcnow()
    started = clock()
    try:
        models = lumen_api.fetch_models(config.lumen_url, config.lumen_api_key, config.refresh.timeout.total_seconds())
        error = None
    except (requests.RequestException, ValueError) as exc:
        models, error = None, str(exc) or type(exc).__name__
    response_time_ms = (clock() - started) * 1000

    model_count = models_up = None
    if models is not None:
        model_count = len(models)
        models_up = sum(1 for m in models if m["status"] != "down")

    with session_factory() as session:
        pruned = _is_first_probe_of_day(session, polled_at, config.timezone)
        if pruned:
            prune(session, polled_at - config.retention)
        session.add(Probe(
            polled_at=polled_at,
            success=models is not None,
            response_time_ms=response_time_ms,
            model_count=model_count,
            models_up=models_up,
        ))
        if models is not None:
            _record_models(session, polled_at, local_day(polled_at, config.timezone), models)
        session.commit()

    if models is not None and snapshot is not None:
        snapshot.set(Snapshot(polled_at, models))
    if error:
        log.warning("probe failed after %.0f ms: %s", response_time_ms, error)
    return ProbeResult(polled_at, models is not None, response_time_ms, model_count, models_up, pruned, error)
