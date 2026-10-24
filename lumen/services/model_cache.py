"""Per-process snapshot of the active models and their healthy endpoints.

Resolving a model and picking an endpoint used to read ``model_configs`` and
``model_endpoints`` on every proxied request. This module keeps both in memory
as frozen plain values (never ORM objects, which cannot outlive the session
that loaded them) so a request can resolve without touching the database.

**Who refreshes it.** Every process holds its own snapshot, so every process
must refresh its own:

* the health loop (``start_health_checker``) refreshes after every tick in
  every process, whether or not that process won the probe election — that is
  how health written by another process, or another replica, reaches this one;
* ``check_all_endpoints`` refreshes after it commits its probe results;
* the config watcher refreshes after a successful model sync;
* the admin Access dialog refreshes after it commits an owner change.

A change made in another process becomes visible here on this process's next
health tick (at most ``health._CHECK_INTERVAL`` later).

**Safety valve.** A process without a health thread (``BACKGROUND_WORKER=false``)
or with a stalled one would never refresh, so a snapshot older than
:data:`MAX_AGE` is reloaded on the next read.

The first load is lazy, on the first read, so CLI commands and throwaway apps
that never serve a model never run the queries.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from flask import current_app
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.model_alias import ModelAlias
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)

#: Seconds after which a snapshot is reloaded on the next read: twice the
#: health interval (``health._CHECK_INTERVAL``; not imported, because health
#: imports this module), so one missed tick is tolerated and two are not.
MAX_AGE = 120


class SnapshotUnavailable(Exception):
    """No snapshot fresh enough to serve, and loading one failed."""


@dataclass(frozen=True)
class ModelSnapshot:
    """The columns of an active ModelConfig that request handling reads.

    Attribute names match ModelConfig so a snapshot can stand in for one
    wherever "anything with its attributes" is accepted (the access check,
    ``stream_target``).
    """

    id: int
    model_name: str
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal
    audio_cost_per_hour: Optional[Decimal]
    owner_entity_id: Optional[int]
    needs_ack: bool
    early_access: bool
    disabled: bool
    end_date: Optional[datetime]

    @property
    def active(self) -> bool:
        """Same rule as ``ModelConfig.active``, evaluated now: an end date can
        pass between two refreshes."""
        return not self.disabled and (self.end_date is None or self.end_date > utcnow())


@dataclass(frozen=True)
class EndpointSnapshot:
    """An active, healthy ModelEndpoint."""

    id: int
    model_config_id: int
    url: str
    api_key: str = field(repr=False)
    model_name: Optional[str]


@dataclass(frozen=True)
class _Snapshot:
    #: Canonical names and aliases, both mapped to the canonical model.
    models: dict[str, ModelSnapshot]
    #: Healthy active endpoints per model_config_id, ordered by id.
    endpoints: dict[int, tuple[EndpointSnapshot, ...]]
    loaded_at: float  # time.monotonic()


#: The current snapshot, or None before the first load. Replaced as a whole by
#: one assignment, so a reader holding a reference always sees a complete one.
_snapshot: Optional[_Snapshot] = None

#: Serialises loads, so an older load can never overwrite a newer one and a
#: burst of readers finding the snapshot stale runs one load, not one each.
_load_lock = threading.Lock()

_rr_lock = threading.Lock()
_rr_counters: dict = {}


def _load() -> _Snapshot:
    """Read the three tables and publish a new snapshot. Caller holds
    ``_load_lock`` and an app context."""
    global _snapshot

    configs = {
        r.id: ModelSnapshot(
            id=r.id,
            model_name=r.model_name,
            input_cost_per_million=r.input_cost_per_million,
            output_cost_per_million=r.output_cost_per_million,
            audio_cost_per_hour=r.audio_cost_per_hour,
            owner_entity_id=r.owner_entity_id,
            needs_ack=r.needs_ack,
            early_access=r.early_access,
            disabled=r.disabled,
            end_date=r.end_date,
        )
        for r in db.session.execute(
            select(
                ModelConfig.id, ModelConfig.model_name,
                ModelConfig.input_cost_per_million, ModelConfig.output_cost_per_million,
                ModelConfig.audio_cost_per_hour, ModelConfig.owner_entity_id,
                ModelConfig.needs_ack, ModelConfig.early_access,
                ModelConfig.disabled, ModelConfig.end_date,
            ).where(ModelConfig.active)
        ).all()
    }
    aliases = db.session.execute(select(ModelAlias.alias, ModelAlias.model_config_id)).all()
    endpoint_rows = db.session.execute(
        select(
            ModelEndpoint.id, ModelEndpoint.model_config_id, ModelEndpoint.url,
            ModelEndpoint.api_key, ModelEndpoint.model_name,
        )
        .where(ModelEndpoint.active.is_(True), ModelEndpoint.healthy.is_(True))
        .order_by(ModelEndpoint.id)
    ).all()
    db.session.rollback()  # end the read transaction; nothing here is kept attached

    # Aliases first, so a canonical name always wins, as in resolve_model_config.
    models = {a.alias: configs[a.model_config_id] for a in aliases if a.model_config_id in configs}
    models.update({mc.model_name: mc for mc in configs.values()})

    endpoints: dict[int, list] = {}
    for r in endpoint_rows:
        if r.model_config_id in configs:
            endpoints.setdefault(r.model_config_id, []).append(EndpointSnapshot(
                id=r.id, model_config_id=r.model_config_id, url=r.url,
                api_key=r.api_key, model_name=r.model_name,
            ))

    _snapshot = _Snapshot(
        models=models,
        endpoints={mc_id: tuple(eps) for mc_id, eps in endpoints.items()},
        loaded_at=time.monotonic(),
    )
    return _snapshot


def refresh(app) -> None:
    """Reload the snapshot from the database. Safe from any thread.

    Runs in its own app context, so it reads only committed data and leaves the
    caller's session alone. Never raises: a failed refresh is logged and the
    previous snapshot stays, to be retried by the next hook or by the safety
    valve, so a refresh can never fail the write that triggered it.
    """
    try:
        with _load_lock, app.app_context():
            _load()
    except Exception:
        logger.exception("model snapshot refresh failed; keeping the previous snapshot")


def clear() -> None:
    """Drop the snapshot so the next read loads it again. For tests."""
    global _snapshot
    with _load_lock:
        _snapshot = None
    with _rr_lock:
        _rr_counters.clear()


def _is_fresh(snap: Optional[_Snapshot]) -> bool:
    return snap is not None and time.monotonic() - snap.loaded_at <= MAX_AGE


def _current() -> _Snapshot:
    """The snapshot, loading it on first use or when older than MAX_AGE.

    Caller owns the app context. A failed load raises SnapshotUnavailable:
    there is nothing older to serve on the first load, and a request should
    not silently run on a snapshot the safety valve has already judged too
    old. Request handlers answer it with a 503.
    """
    snap = _snapshot
    if _is_fresh(snap):
        return snap
    with _load_lock:
        snap = _snapshot  # another reader may have loaded it while we waited
        if _is_fresh(snap):
            return snap
        try:
            with current_app.app_context():
                return _load()
        except Exception as exc:
            raise SnapshotUnavailable("model snapshot could not be loaded") from exc


def resolve(name: str) -> Optional[ModelSnapshot]:
    """The active model for a canonical name or alias, or None.

    Snapshot counterpart of ``resolve_model_config``. Raises
    SnapshotUnavailable when no usable snapshot can be loaded.
    """
    if not name:
        return None
    mc = _current().models.get(name)
    return mc if mc is not None and mc.active else None


def endpoints(model_config_id: int) -> tuple[EndpointSnapshot, ...]:
    """The healthy active endpoints of a model, ordered by id."""
    return _current().endpoints.get(model_config_id, ())


def next_endpoint(model_config_id: int) -> Optional[EndpointSnapshot]:
    """The next healthy endpoint for a model using round-robin, or None.

    Snapshot counterpart of ``llm.get_next_endpoint``.
    """
    eps = endpoints(model_config_id)
    if not eps:
        return None
    with _rr_lock:
        idx = _rr_counters.get(model_config_id, 0) % len(eps)
        _rr_counters[model_config_id] = idx + 1
    return eps[idx]
