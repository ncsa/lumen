import logging
import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import NamedTuple, Optional

import openai
from flask import current_app, has_request_context, request
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from lumen.blueprints.metrics.middleware import observe_stream_abort
from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_group_access import ModelGroupAccess
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.services.crypto import cache_salt_for_entity
from lumen.services.wsgi_disconnect import (
    SEND_BLOCKED_ENVIRON_KEY,
    STARTED_AT_ENVIRON_KEY,
    T0_ENVIRON_KEY,
    SendBlocked,
    client_disconnect_event,
)
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)

# Sentinel for "argument not supplied" so callers can pass an explicit None.
_UNSET = object()


class _greatest(FunctionElement):
    """SQL GREATEST(...); compiles to scalar max(...) on SQLite, which has no GREATEST."""
    name = "greatest"
    inherit_cache = True


@compiles(_greatest)
def _greatest_default(element, compiler, **kw):
    return "greatest(%s)" % compiler.process(element.clauses, **kw)


@compiles(_greatest, "sqlite")
def _greatest_sqlite(element, compiler, **kw):
    return "max(%s)" % compiler.process(element.clauses, **kw)


class _least(FunctionElement):
    """SQL LEAST(...); compiles to scalar min(...) on SQLite, which has no LEAST."""
    name = "least"
    inherit_cache = True


@compiles(_least)
def _least_default(element, compiler, **kw):
    return "least(%s)" % compiler.process(element.clauses, **kw)


@compiles(_least, "sqlite")
def _least_sqlite(element, compiler, **kw):
    return "min(%s)" % compiler.process(element.clauses, **kw)

#: How old ``entity_balances.last_refill_at`` must be before the refiller
#: credits that balance again — the cutoff in ``token_refill.refill_coin_balances``,
#: which is what makes the next refill instant derivable here. Keep the two in step.
REFILL_INTERVAL = timedelta(hours=1)


def observe_rejection_quietly(reason: str, source: str, model: str = "") -> None:
    """Count a rejection, never at the cost of the response.

    Same shape (and same reason) as ``_observe_rejection_quietly`` in
    ``lumen/__init__.py``: a counter that cannot be incremented is not a reason
    to fail a request that was already being rejected cleanly.

    Never *triggers* the import of the metrics middleware — the same rule (and
    the same ``sys.modules`` lookup) as ``pool_tracker._observe_wait`` and
    ``wsgi_disconnect._observe_shed``: prometheus_client binds each metric to
    its mmap file at construction, so an import landing before
    PROMETHEUS_MULTIPROC_DIR is set produces metrics no scrape will ever merge.
    Looking the module up keeps this a no-op until the app has imported it in
    the right order. Rebinding ``observe_rejection`` on the module (as the tests
    do) still reaches this call site, because the attribute lookup happens here
    at call time.
    """
    middleware = sys.modules.get("lumen.blueprints.metrics.middleware")
    if middleware is None:
        return
    try:
        middleware.observe_rejection(reason, source, model)
    except Exception:  # noqa: BLE001 - instrumentation must never escalate
        pass


def upstream_call_bounds(*, streaming: bool):
    """Return (timeout, max_retries) for an upstream ``openai.OpenAI`` client.

    ``streaming`` selects which read bound applies, and they mean genuinely
    different things — one value cannot serve both. On a streaming call the read
    timeout is the maximum gap *between chunks*, so a low value is safe however
    long the generation runs. On a non-streaming call the same setting bounds the
    entire wait for the response body, and a non-streaming completion or a long
    audio transcription can legitimately take minutes. Sharing one number would
    either leave streams unbounded or start failing slow non-streaming requests
    that work today.

    Every proxy client must be bounded. The SDK's own defaults are 600 s and
    two retries, so a silently stalled backend can pin a WSGI worker thread for
    ~30 minutes across three attempts of a single client request — long past
    the gateway deadline that made those attempts orphan work.

    ``openai.Timeout`` is ``httpx.Timeout`` re-exported; a structured timeout
    rather than a bare float so connecting and reading are bounded separately.

    Must be called while an application context is current; streaming callers
    capture the result into their generator's closure, since the generator runs
    context-free and has no ``current_app``.
    """
    cfg = current_app.config
    connect = float(cfg.get("LLM_CONNECT_TIMEOUT", 5.0))
    if streaming:
        read = float(cfg.get("LLM_READ_TIMEOUT", 300.0))
        # Never auto-retry a stream: a retry restarts the whole generation while
        # the first attempt may still be draining upstream, which is duplicate
        # backend work for one client request. Not configurable for that reason.
        max_retries = 0
    else:
        read = float(cfg.get("LLM_REQUEST_TIMEOUT", 600.0))
        max_retries = int(cfg.get("LLM_MAX_RETRIES", 1))
    # write is bounded like read (it covers pushing the request body, e.g. an
    # audio upload); pool is bounded like connect, and is near-instant anyway
    # because every call site builds its own single-use client.
    return openai.Timeout(connect=connect, read=read, write=read, pool=connect), max_retries


def _resolve_single_access(
    is_owner: bool,
    owner_entity_id,
    granted: bool,
    model_needs_ack: bool = False,
    model_disabled: bool = False,
    model_early_access: bool = False,
    model_end_date=None,
) -> str:
    """Resolve 'allowed', 'needs_ack', or 'blocked' from pre-fetched per-model access data.

    - 'disabled' models and models past their end_date short-circuit to 'blocked'
      and cannot be overridden.
    - A model without an owner (owner_entity_id is None) is visible to everyone;
      an owned model is visible only to its owner and to members of groups the
      model has been granted to (granted).
    - 'needs_ack' means the model has acknowledgement requirements (needs_ack
      and/or early_access); these are model-level properties.
    """
    if model_disabled or (model_end_date is not None and model_end_date <= utcnow()):
        return "blocked"
    if owner_entity_id is not None and not is_owner and not granted:
        return "blocked"
    return "needs_ack" if (model_needs_ack or model_early_access) else "allowed"


class PoolLimit(NamedTuple):
    max_coins: float
    refresh_coins: float
    starting_coins: float


def best_group_pool_limit(group_limits):
    """Pick the best coin pool from a set of GroupLimit rows, or None if none usable.

    Skips limits with max_coins == 0 (blocking); an unlimited limit (-2) wins;
    otherwise the highest max_coins wins.
    """
    candidates = [
        PoolLimit(float(gl.max_coins), float(gl.refresh_coins), float(gl.starting_coins))
        for gl in group_limits if float(gl.max_coins) != 0
    ]
    if not candidates:
        return None
    for c in candidates:
        if c.max_coins == -2:
            return PoolLimit(-2, 0, 0)
    return max(candidates, key=lambda x: x.max_coins)


def bulk_model_access_info(entity_id: int, model_config_ids: list) -> tuple:
    """
    Bulk-resolve model access status and consents for one entity across many models.

    Returns (access_statuses, consent_map) where access_statuses is a dict
    {model_config_id: "allowed"|"blocked"|"needs_ack"} and consent_map is a dict
    {model_config_id: acknowledged_at} for models where the entity has satisfied
    ALL of the model's acknowledgement requirements (needs_ack and early_access);
    the value is the most recent acknowledgement timestamp.

    Issues a fixed set of queries regardless of the number of models — replaces N+1
    per-model calls to get_model_access_status / has_model_consent.
    """
    if not model_config_ids:
        return {}, {}

    baseline = {
        r.id: r
        for r in db.session.execute(
            select(ModelConfig.id, ModelConfig.owner_entity_id, ModelConfig.needs_ack, ModelConfig.disabled,
                   ModelConfig.early_access, ModelConfig.end_date)
            .where(ModelConfig.id.in_(model_config_ids))
        ).all()
    }
    # Ids that don't resolve to a model are a caller bug; resolving them to a
    # status here would silently fail open (no owner row -> public).
    unknown = set(model_config_ids) - baseline.keys()
    if unknown:
        raise ValueError(f"unknown model_config_id(s): {sorted(unknown)}")

    # Group grants only matter for owned models the entity does not own itself.
    granted_ids: set = set()
    if any(r.owner_entity_id is not None and r.owner_entity_id != entity_id for r in baseline.values()):
        group_ids = _get_active_group_ids(entity_id)
        if group_ids:
            granted_ids = {
                mc_id for (mc_id,) in db.session.execute(
                    select(ModelGroupAccess.model_config_id).where(
                        ModelGroupAccess.group_id.in_(group_ids),
                        ModelGroupAccess.model_config_id.in_(model_config_ids),
                    )
                ).all()
            }

    # Only models whose acknowledgement requirements are ALL satisfied appear in
    # consent_map; a requirement added after consent leaves its timestamp NULL,
    # so the model drops out and the UI re-prompts.
    consent_map = {}
    for r in db.session.execute(
        select(EntityModelConsent).where(
            EntityModelConsent.entity_id == entity_id,
            EntityModelConsent.model_config_id.in_(model_config_ids),
        )
    ).scalars().all():
        mc = baseline.get(r.model_config_id)
        if _consent_satisfied(r, mc.needs_ack if mc else False, mc.early_access if mc else False):
            times = [t for t in (r.consented_at, r.early_access_at) if t is not None]
            if times:
                consent_map[r.model_config_id] = max(times)

    access_statuses: dict = {}
    for mc_id in model_config_ids:
        mc = baseline[mc_id]
        access_statuses[mc_id] = _resolve_single_access(
            mc.owner_entity_id == entity_id,
            mc.owner_entity_id,
            mc_id in granted_ids,
            model_needs_ack=mc.needs_ack,
            model_disabled=mc.disabled,
            model_early_access=mc.early_access,
            model_end_date=mc.end_date,
        )

    return access_statuses, consent_map


def get_model_status(mc) -> str:
    """Return 'ok', 'degraded', 'down', or 'disabled' for a ModelConfig."""
    if not mc.active:
        return "disabled"
    endpoints = list(mc.endpoints)
    if not endpoints:
        return "down"
    healthy = sum(1 for e in endpoints if e.healthy)
    if healthy == 0:
        return "down"
    if healthy < len(endpoints):
        return "degraded"
    return "ok"

_rr_counters: dict = {}
_rr_lock = threading.Lock()


def get_next_endpoint(model_config_id: int):
    """Return the next healthy endpoint for a model using round-robin, or None."""
    endpoints = db.session.execute(
        select(ModelEndpoint).filter_by(model_config_id=model_config_id, healthy=True).order_by(ModelEndpoint.id)
    ).scalars().all()
    if not endpoints:
        return None
    with _rr_lock:
        idx = _rr_counters.get(model_config_id, 0) % len(endpoints)
        _rr_counters[model_config_id] = idx + 1
    return endpoints[idx]


def _get_active_group_ids(entity_id: int) -> list:
    """Return list of active group IDs the entity belongs to."""
    return [
        m.group_id for m in db.session.execute(
            select(GroupMember)
            .join(Group, Group.id == GroupMember.group_id)
            .where(GroupMember.entity_id == entity_id, Group.active == True)  # noqa: E712
        ).scalars().all()
    ]


def get_model_access_status(entity_id: int, model_config_id: int) -> str:
    """
    Return 'allowed', 'blocked', or 'needs_ack' for the given entity + model.

    A model without an owner is allowed for everyone; an owned model is allowed
    only for its owner and members of groups it has been granted to.
    """
    access_statuses, _ = bulk_model_access_info(entity_id, [model_config_id])
    return access_statuses[model_config_id]


def _consent_satisfied(row, needs_ack: bool, early_access: bool) -> bool:
    """True if a consent row covers all of a model's acknowledgement requirements."""
    if row is None:
        return False
    return (not needs_ack or row.consented_at is not None) and \
           (not early_access or row.early_access_at is not None)


def has_model_consent(entity_id: int, model_config_id: int) -> bool:
    """Return True if the entity has satisfied all of the model's acknowledgement requirements."""
    row = db.session.execute(
        select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=model_config_id)
    ).scalar_one_or_none()
    mc = db.session.get(ModelConfig, model_config_id)
    return _consent_satisfied(row, mc.needs_ack if mc else False, mc.early_access if mc else False)


def model_notices(mc) -> tuple:
    """Return (ack_notice, early_access_notice) markdown strings for a ModelConfig.

    ack_notice is set iff the model has needs_ack (per-model message falling back
    to defaults.models.ack_message); early_access_notice is set iff the model is
    early_access (global defaults.models.early_access_message)."""
    defaults = current_app.config.get("MODEL_DEFAULTS", {})
    ack_notice = (mc.ack_message or defaults.get("ack_message")) if mc.needs_ack else None
    early_notice = defaults.get("early_access_message") if mc.early_access else None
    return ack_notice, early_notice


def get_model_access(entity_id: int, model_config_id: int, require_consent: bool = True) -> bool:
    """
    Return True if entity can access the given model, False otherwise.

    For models that require acknowledgement, requires prior consent (EntityModelConsent)
    unless require_consent is False (used to exempt API requests from the consent gate).
    """
    status = get_model_access_status(entity_id, model_config_id)
    if status == "blocked":
        return False
    if status == "needs_ack":
        if not require_consent:
            return True
        return has_model_consent(entity_id, model_config_id)
    return True


def get_pool_limit(entity_id: int):
    """
    Return (max_coins, refresh_coins, starting_coins) for entity's coin pool, or None if blocked.

    max_coins == -2 means unlimited.

    Resolution: user's EntityLimit always wins over group limits (consistent with model access).
    If no EntityLimit exists, fall back to the best GroupLimit (-2 beats any positive value),
    then to the global defaults.tokens pool. EntityLimit with max_coins == 0 blocks the entity
    regardless of groups.
    """
    user_limit = db.session.execute(select(EntityLimit).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if user_limit is not None:
        if float(user_limit.max_coins) == 0:
            return None  # explicitly blocked
        return PoolLimit(float(user_limit.max_coins), float(user_limit.refresh_coins), float(user_limit.starting_coins))

    # No user-level limit — fall back to best group limit, then the global default pool.
    group_ids = _get_active_group_ids(entity_id)
    if group_ids:
        group_limits = db.session.execute(
            select(GroupLimit).where(GroupLimit.group_id.in_(group_ids))
        ).scalars().all()
        best = best_group_pool_limit(group_limits)
        if best is not None:
            return best

    td = current_app.config.get("TOKEN_DEFAULTS")
    if td and float(td["max"]) != 0:
        return PoolLimit(float(td["max"]), float(td["refresh"]), float(td["starting"]))
    return None


def get_effective_limit(entity_id: int, model_config_id: int, require_consent: bool = True):
    """
    Return (max_coins, refresh_coins, starting_coins) or None if blocked/no access.

    Checks model access first, then returns the entity's coin pool.
    max_coins == -2 means unlimited.
    """
    if not get_model_access(entity_id, model_config_id, require_consent=require_consent):
        return None
    return get_pool_limit(entity_id)


def get_coin_balance(entity_id: int, model_config_id: int):
    """Return coins_left for entity's pool, or None if unlimited or blocked."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return None
    max_coins, _, starting = effective
    if max_coins == -2:
        return None

    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if balance is None:
        return float(starting)

    return float(balance.coins_left)


def subtract_coins(entity_id: int, model_config_id: int, coin_cost: float, effective=_UNSET):
    """Deduct coin_cost from the entity's pool balance (no-op for unlimited or blocked).

    Pass ``effective`` (the limit already resolved by check_coin_budget/get_effective_limit
    during preflight) to skip re-resolving model access and the coin pool in the hot
    billing path. When omitted it is resolved here.
    """
    if effective is _UNSET:
        effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return
    max_coins, _refresh, starting = effective
    if max_coins == -2:
        return

    # Ensure balance row exists (first API use before login creates it).
    # Check first: the row exists for every entity after its first request, so an
    # unconditional INSERT would fail — and log a Postgres duplicate-key ERROR —
    # on every subsequent request. Same race-safe pattern as update_stats below:
    # if two concurrent first-requests both see None, the loser's IntegrityError
    # is swallowed and the atomic UPDATE succeeds for both.
    if db.session.execute(
        select(EntityBalance).filter_by(entity_id=entity_id)
    ).scalar_one_or_none() is None:
        try:
            with db.session.begin_nested():
                db.session.add(EntityBalance(
                    entity_id=entity_id,
                    coins_left=starting,
                    last_refill_at=utcnow(),
                ))
        except IntegrityError:
            pass

    # Single atomic deduction, floored at 0: deduct when affordable, otherwise zero
    # (the budget is a soft limit — see check_coin_budget). One statement so a
    # concurrent refill/credit can never be clobbered back to 0 by a separate zeroing.
    db.session.execute(
        sa_update(EntityBalance)
        .where(EntityBalance.entity_id == entity_id)
        .values(coins_left=_greatest(0, EntityBalance.coins_left - coin_cost))
    )
    db.session.flush()


def _observe_denial_quietly(entity_id: int, model_config_id: int, require_consent: bool,
                            source: str, model_name: str) -> None:
    """Count a 403 under the reason it was actually decided for.

    ``get_effective_limit`` collapses "blocked" and "requires an acknowledgement
    nobody has given" into one None, and the taxonomy needs them apart: the
    second is the user's to fix from the model detail page, the first is not.
    Re-resolving the status costs a query on a path that is already refusing the
    request. Never raises, for the same reason ``observe_rejection_quietly``
    does not.
    """
    try:
        needs_consent = (
            require_consent
            and get_model_access_status(entity_id, model_config_id) == "needs_ack"
            and not has_model_consent(entity_id, model_config_id)
        )
    except Exception:  # noqa: BLE001 - instrumentation must never escalate
        return
    observe_rejection_quietly("needs_consent" if needs_consent else "no_access", source, model_name)


def check_coin_budget(entity_id: int, model_config_id: int, require_consent: bool = True,
                      source: str = None, model_name: str = ""):
    """Check coin budget. Returns (ok, http_code, error_message, effective).

    ``effective`` is the resolved coin pool limit (or None); pass it to subtract_coins
    afterward to avoid re-resolving model access and the pool limit per request.

    ``source`` ("chat" or "api", matching ``request_logs.source``) and
    ``model_name`` are only used to label the rejection counter; pass them from
    the view, which is the only caller that knows which surface it is serving.
    Omitting ``source`` skips the counting entirely, which is what callers that
    are not serving a request (tests, admin tooling) want.

    This is an optimistic gate: it checks that the balance is > 0 before the LLM
    call, but the actual cost is unknown until the call completes. A user with a tiny
    positive balance will pass this check, consume tokens, and have their balance
    zeroed by subtract_coins afterward. This is intentional — the budget is a soft
    spending limit, not a hard reservation.
    """
    effective = get_effective_limit(entity_id, model_config_id, require_consent=require_consent)
    if effective is None:
        if source:
            _observe_denial_quietly(entity_id, model_config_id, require_consent, source, model_name)
        return False, HTTPStatus.FORBIDDEN, "No access to this model", None
    max_coins, _, _starting = effective
    if max_coins == -2:
        return True, None, None, effective
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if balance is not None and float(balance.coins_left) <= 0:
        if source:
            observe_rejection_quietly("coin_budget", source, model_name)
        return False, HTTPStatus.TOO_MANY_REQUESTS, "Coin budget exhausted", None
    return True, None, None, effective


def coin_retry_after(entity_id: int) -> Optional[int]:
    """Seconds until this entity's coin balance is next credited, or None.

    ``refill_coin_balances`` credits a balance once its ``last_refill_at`` is an
    hour old (it runs every 60s, so that instant is the earliest, not the exact
    moment), which makes the next refill a real derivable time rather than a
    guessed one. None means no refill is coming and the caller must send no
    ``Retry-After`` at all: an unlimited or blocked pool, a pool whose
    ``refresh_coins`` is 0 (it never refills — the balance only moves when an
    admin changes it), or an entity with no balance row yet.

    Never raises: a header that cannot be derived must degrade to a missing
    header, not to a 500 on a request that was being refused cleanly.
    """
    try:
        pool = get_pool_limit(entity_id)
        if pool is None:
            return None
        max_coins, refresh_coins, _starting = pool
        if max_coins == -2 or refresh_coins <= 0:
            return None
        balance = db.session.execute(
            select(EntityBalance).filter_by(entity_id=entity_id)
        ).scalar_one_or_none()
        if balance is None or balance.last_refill_at is None:
            return None
        last_refill = balance.last_refill_at
        if last_refill.tzinfo is not None:
            # last_refill_at is documented as UTC; a stray aware value is
            # interpreted as such, so normalize rather than discarding an offset
            # that means something else.
            last_refill = last_refill.astimezone(timezone.utc).replace(tzinfo=None)
        due_in = (last_refill + REFILL_INTERVAL - utcnow()).total_seconds()
        # Floored at 1: a refill already due arrives within the refiller's next
        # 60s pass, and "come back in 0 seconds" is an invitation to hot-loop.
        return max(1, math.ceil(due_in))
    except Exception:  # noqa: BLE001 - a missing header beats a failed response
        logger.debug("could not derive Retry-After from the coin balance", exc_info=True)
        return None


#: WSGI environ key holding the seconds the request waited for a WSGI worker.
#: Written by the ``before_request`` hook in ``lumen/__init__.py``, which
#: subtracts the bridge's T0 mark on pickup.
QUEUE_WAIT_ENVIRON_KEY = "lumen.queue_wait"


class RequestTiming(NamedTuple):
    """The per-request marks the ASGI bridge publishes, captured in the view.

    Read it with :func:`capture_request_timing` while the request context is
    still current and pass it into the streaming generators as a parameter: the
    generators run context-free and must never touch ``request`` (the same rule
    ``client_disconnect_event`` documents).

    Every field is None when the request did not come through the bridge — the
    Werkzeug dev server, the Flask test client, direct unit-test calls. None
    stores SQL NULL, which means "not measured" and stays distinguishable from a
    measured zero.

    ``send_blocked`` is the live holder rather than a float on purpose: on the
    streaming paths nothing has been sent when the view captures this, so only a
    read taken at billing time carries a total.
    """

    started_at: Optional[datetime] = None
    queue_wait: Optional[float] = None
    #: T1 — the monotonic instant a worker thread picked the request up.
    picked_up_at: Optional[float] = None
    send_blocked: Optional[SendBlocked] = None

    def preflight(self, upstream_t0: Optional[float]) -> Optional[float]:
        """Seconds from worker pickup (T1) to the upstream call (T2)."""
        if self.picked_up_at is None or upstream_t0 is None:
            return None
        return upstream_t0 - self.picked_up_at

    def blocked_seconds(self) -> Optional[float]:
        """The send-blocked total as it stands now; None when there is no holder."""
        return None if self.send_blocked is None else self.send_blocked.seconds


def capture_request_timing() -> RequestTiming:
    """Read this request's bridge marks out of the WSGI environ.

    Call it from view code while the request context is live, never from a
    streaming generator. Never raises: the keys are absent on every non-ASGI
    deployment, and a missing mark must degrade to "not measured" rather than to
    a 500 on a path that is meant to be a pure observability improvement.
    """
    environ = request.environ if has_request_context() else {}
    t0 = environ.get(T0_ENVIRON_KEY)
    queue_wait = environ.get(QUEUE_WAIT_ENVIRON_KEY)
    return RequestTiming(
        started_at=environ.get(STARTED_AT_ENVIRON_KEY),
        queue_wait=queue_wait,
        # T1 is not published directly — it is the arrival mark plus the wait
        # the before_request hook measured against it.
        picked_up_at=None if t0 is None or queue_wait is None else t0 + queue_wait,
        send_blocked=environ.get(SEND_BLOCKED_ENVIRON_KEY),
    )


def update_stats(
    entity_id: int,
    model_config_id: int,
    source: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    endpoint_id: int = None,
    duration: float = 0.0,
    audio_seconds: int = 0,
    aborted: bool = False,
    timing: RequestTiming = RequestTiming(),
    upstream_t0: float = None,
    ttft: float = None,
    ttft_visible: float = None,
    outcome: str = None,
):
    """Update or create ModelStat/EntityStat running totals and append a RequestLog row.

    ``aborted`` marks the RequestLog row as one whose stream ended before the
    client had read it; its token counts may be estimated (see
    estimate_abort_usage).

    ``timing`` carries the arrival marks captured in the view (see
    RequestTiming) and ``upstream_t0`` is the monotonic instant taken
    immediately before the upstream call, from which the preflight span is
    derived. The timing columns ride the RequestLog INSERT that already runs, so
    none of this adds a statement. Anything not measured stays None and is
    stored as SQL NULL — deliberately not zero.
    """
    now = utcnow()

    # Ensure ModelStat row exists before the atomic increment.
    # Use try/except to handle the race where two concurrent first-requests both
    # see None and both attempt the INSERT — the loser gets IntegrityError which
    # we swallow; the atomic UPDATE below then succeeds for both.
    if db.session.execute(
        select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_config_id, source=source)
    ).scalar_one_or_none() is None:
        try:
            with db.session.begin_nested():
                db.session.add(ModelStat(
                    entity_id=entity_id, model_config_id=model_config_id, source=source,
                    requests=0, input_tokens=0, output_tokens=0, cost=0,
                ))
        except IntegrityError:
            pass

    db.session.execute(
        sa_update(ModelStat)
        .where(ModelStat.entity_id == entity_id, ModelStat.model_config_id == model_config_id, ModelStat.source == source)
        .values(
            requests=ModelStat.requests + 1,
            input_tokens=ModelStat.input_tokens + input_tokens,
            output_tokens=ModelStat.output_tokens + output_tokens,
            audio_seconds=ModelStat.audio_seconds + audio_seconds,
            cost=ModelStat.cost + cost,
            last_used_at=now,
        )
    )

    # Ensure EntityStat row exists before the atomic increment.
    # Same race-safe pattern as ModelStat above.
    if db.session.execute(select(EntityStat).filter_by(entity_id=entity_id)).scalar_one_or_none() is None:
        try:
            with db.session.begin_nested():
                db.session.add(EntityStat(entity_id=entity_id, requests=0, input_tokens=0, output_tokens=0, cost=0))
        except IntegrityError:
            pass

    db.session.execute(
        sa_update(EntityStat)
        .where(EntityStat.entity_id == entity_id)
        .values(
            requests=EntityStat.requests + 1,
            input_tokens=EntityStat.input_tokens + input_tokens,
            output_tokens=EntityStat.output_tokens + output_tokens,
            audio_seconds=EntityStat.audio_seconds + audio_seconds,
            cost=EntityStat.cost + cost,
            last_used_at=now,
        )
    )

    log = RequestLog(
        time=datetime.now(timezone.utc),
        entity_id=entity_id,
        model_config_id=model_config_id,
        model_endpoint_id=endpoint_id,
        source=source,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        audio_seconds=audio_seconds,
        cost=cost,
        duration=duration,
        aborted=aborted,
        started_at=timing.started_at,
        queue_wait=timing.queue_wait,
        preflight=timing.preflight(upstream_t0),
        ttft=ttft,
        ttft_visible=ttft_visible,
        # Read here rather than captured in the view: on the streaming paths
        # nothing had been sent yet when the view ran.
        send_blocked=timing.blocked_seconds(),
        outcome=outcome,
    )
    db.session.add(log)
    db.session.flush()


# Characters per token for the prompt fallback estimate. The upstream reports the
# exact prompt_tokens only in its terminal usage chunk, which an aborted stream
# never reaches, and Lumen has no tokenizer for the (arbitrary, per-endpoint) model.
_CHARS_PER_TOKEN = 4


def estimate_prompt_tokens(messages) -> int:
    """Estimate prompt tokens from the request messages at ~4 characters per token.

    Multimodal content arrives as a list of parts; only their text is counted, so a
    prompt carrying images or audio is under-estimated.
    """
    chars = 0
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chars += len(part["text"])
    return round(chars / _CHARS_PER_TOKEN)


def estimate_abort_usage(usage, messages, content_deltas, in_cost_per_million, out_cost_per_million):
    """Return (input_tokens, output_tokens, cost) for a stream that ended early.

    Prefers the upstream's exact totals when the terminal usage chunk had already
    arrived — both streaming loops capture ``chunk.usage`` before they test the
    disconnect flag precisely so a disconnect landing in that window keeps the real
    figures. Otherwise both counts are estimates: the prompt from its character
    count, and the output as one token per content delta received, the ratio most
    OpenAI-compatible backends emit.
    """
    if usage is not None:
        input_tokens = usage.prompt_tokens or 0
        output_tokens = usage.completion_tokens or 0
    else:
        input_tokens = estimate_prompt_tokens(messages)
        output_tokens = content_deltas
    cost = round(
        input_tokens * in_cost_per_million / 1_000_000
        + output_tokens * out_cost_per_million / 1_000_000,
        6,
    )
    return input_tokens, output_tokens, cost


def record_aborted_request(entity_id, model_config_id, source, endpoint_id=None, duration=0.0,
                           input_tokens=0, output_tokens=0, cost=0.0,
                           effective=_UNSET, record_extra=None,
                           timing=RequestTiming(), upstream_t0=None, ttft=None, ttft_visible=None):
    """Bill and log a request_logs row for a stream the client abandoned mid-response.

    Billed exactly like a completed request — coins deducted and running totals
    updated — because the backend really produced the tokens the client walked away
    from; leaving it free is an unlimited free-inference method. The counts are the
    upstream's exact totals when its terminal usage chunk had arrived, and otherwise
    the estimate from estimate_abort_usage.

    The row is marked ``aborted``, which is how mid-stream disconnects are monitored.
    That replaces the old "cost == 0 identifies an abort" convention, which stopped
    being unique the moment aborted requests started carrying a real cost.

    ``record_extra``, if given, runs inside the same session before the commit, for
    accounting the caller owns (the API path's per-API-key totals).

    Best-effort: never raise into the (already closing) generator.
    """
    try:
        subtract_coins(entity_id, model_config_id, cost, effective=effective)
        update_stats(
            entity_id, model_config_id, source,
            input_tokens, output_tokens, cost,
            endpoint_id=endpoint_id, duration=duration, aborted=True,
            timing=timing, upstream_t0=upstream_t0,
            ttft=ttft, ttft_visible=ttft_visible, outcome="disconnect",
        )
        if record_extra is not None:
            record_extra()
        db.session.commit()
    except Exception:
        logger.exception("failed to record aborted request (entity_id=%s, model=%s)", entity_id, model_config_id)
        db.session.rollback()


def record_stream_abort(app, *, billed, entity_id, model_config_id, source, endpoint_id, stream_t0,
                        input_tokens=0, output_tokens=0, cost=0.0, effective=_UNSET, record_extra=None,
                        reason="disconnect", timing=RequestTiming(), ttft=None, ttft_visible=None):
    """Abort accounting for a streaming generator that ends before billing.

    Every path that can end a stream early shares this, so they cannot drift:
    the ``GeneratorExit`` raised when the response iterable is closed, and the
    polled client-disconnect flag. Does nothing once billing has completed.

    ``reason`` labels the ``lumen_stream_aborts_total`` counter. It is passed in
    rather than inferred here: both of this function's callers arrive from a
    client going away, so nothing distinguishable is available at this seam.

    Pushes its own short-lived application context because the streaming
    generators run context-free; call it only from a point with no ``yield``
    in scope, so no context can span one. Best-effort, like the
    ``record_aborted_request`` it wraps: the caller is usually a generator that
    is already unwinding, so a failure here (including one raised by the
    context teardown's session release) must not replace the original exit.
    """
    if billed:
        return
    # Counted before the anonymous-stream guard so the metric measures aborts,
    # not billable aborts — and outside the try below so a DB failure still
    # leaves the abort visible.
    observe_stream_abort(source, reason)
    if entity_id is None:
        return
    try:
        with app.app_context():
            record_aborted_request(
                entity_id, model_config_id, source,
                endpoint_id=endpoint_id, duration=time.monotonic() - stream_t0,
                input_tokens=input_tokens, output_tokens=output_tokens, cost=cost,
                effective=effective, record_extra=record_extra,
                timing=timing, upstream_t0=stream_t0,
                ttft=ttft, ttft_visible=ttft_visible,
            )
    except Exception:
        logger.exception("abort accounting failed (entity_id=%s, model=%s)", entity_id, model_config_id)


def send_message_stream(
    messages: list,
    model: str,
    entity_id: int = None,
    source: str = "chat",
    effective=_UNSET,
):
    """Stream messages to LLM. Yields (chunk_text, None) for each token, then (None, result_dict).

    ``effective`` is the coin pool limit already resolved during preflight; it is
    threaded to subtract_coins to avoid re-resolving it after the stream completes.

    Must be *called* inside an application context (view code); the returned
    generator runs without one. Each DB phase pushes its own short-lived app
    context that never spans a ``yield``, so no session is ever keyed to a
    context that outlives the phase — Flask's stream_with_context instead
    re-pushes the request's context onto whichever worker thread iterates the
    body, and an abandoned generator leaves it stuck there, poisoning every
    later request on that thread.

    The client-disconnect flag is captured here, while the request context is
    still current, and polled inside the generator. GeneratorExit alone is not
    enough: under uvicorn + a2wsgi the response generator is never closed on a
    disconnect, so that handler never fires in production.
    """
    app = current_app._get_current_object()
    disconnected = client_disconnect_event()
    # Same reason as the disconnect Event: the arrival marks live in the WSGI
    # environ and only the view can reach them.
    timing = capture_request_timing()
    return _send_message_stream(app, messages, model, entity_id, source, effective, disconnected, timing)


def _send_message_stream(app, messages, model, entity_id, source, effective, disconnected, timing):
    with app.app_context():
        config = db.session.execute(select(ModelConfig).where(ModelConfig.model_name == model, ModelConfig.active)).scalar_one_or_none()
        if config is None:
            raise ValueError(f"Unknown or inactive model: {model}")

        endpoint = get_next_endpoint(config.id)
        if endpoint is None:
            observe_rejection_quietly("no_healthy_endpoint", source, model)
            raise RuntimeError(f"No healthy endpoints for model '{model}'")

        # Extract all scalars from ORM objects before the context exits. The
        # streaming LLM call can take minutes; the context teardown releases the
        # session (and its pool connection) before the first token is awaited.
        remote_model = endpoint.model_name or model
        ep_api_key   = endpoint.api_key
        ep_url       = endpoint.url
        ep_id        = endpoint.id
        mc_id        = config.id
        mc_in_cost   = float(config.input_cost_per_million)
        mc_out_cost  = float(config.output_cost_per_million)
        # Derive the per-entity prefix-cache salt while a context is current;
        # the create() call below runs context-free (no current_app). See #36.
        cache_salt   = cache_salt_for_entity(entity_id) if entity_id is not None else None
        # Same reason: read the upstream bounds here, into plain scalars in this
        # generator's frame. openai.OpenAI() below is constructed after this
        # context has exited, where current_app does not exist.
        timeout, max_retries = upstream_call_bounds(streaming=True)

    t0 = time.monotonic()
    t_first = None
    # First chunk of any kind, reasoning included — ttft, where t_first is
    # ttft_visible. On a reasoning model the two differ by the thinking phase.
    t_first_any = None
    parts = []
    usage = None
    billed = False
    aborted = False
    # Which stage a failure came from, for the abort metric's reason label.
    phase = "upstream"

    def _abort():
        """Bill and log what this stream consumed before the client went away.

        Reads ``usage``/``parts`` at call time, so it reflects however far the
        stream got. Shared by the break-on-disconnect path and GeneratorExit so
        the two cannot bill differently.
        """
        input_tokens, output_tokens, cost = estimate_abort_usage(
            usage, messages, len(parts), mc_in_cost, mc_out_cost)
        record_stream_abort(
            app, billed=billed, entity_id=entity_id, model_config_id=mc_id,
            source=source, endpoint_id=ep_id, stream_t0=t0,
            input_tokens=input_tokens, output_tokens=output_tokens, cost=cost,
            effective=effective,
            timing=timing, ttft=t_first_any, ttft_visible=t_first,
        )

    try:
        # max_retries=0: a streaming call is never auto-retried. A retry
        # restarts the whole generation while the first attempt may still be
        # draining upstream, so one client request becomes two backend
        # generations — and the tokens already streamed to the client cannot be
        # un-sent. LLM_MAX_RETRIES applies to the non-streaming paths only.
        #
        # The read timeout here is the maximum gap *between* chunks, so a long
        # generation is unaffected. It is also what bounds F10: the disconnect
        # flag is only polled between chunks, so while blocked in the upstream's
        # __next__() a disconnect cannot be observed at all — the read timeout
        # caps that blind window instead of leaving it unbounded.
        with openai.OpenAI(api_key=ep_api_key, base_url=ep_url,
                           timeout=timeout, max_retries=max_retries) as client:
            stream = client.chat.completions.create(
                model=remote_model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                extra_body={"cache_salt": cache_salt} if cache_salt else {},
            )

            thinking_parts = []
            for chunk in stream:
                if t_first_any is None:
                    t_first_any = time.monotonic() - t0
                # Capture usage before testing the flag. The totals ride on the
                # terminal chunk, so a disconnect landing in that same window
                # would otherwise throw away figures already in hand — and bill
                # nothing for work the backend actually did.
                if chunk.usage:
                    usage = chunk.usage
                if disconnected.is_set():
                    aborted = True
                    break
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                    if reasoning:
                        thinking_parts.append(reasoning)
                        yield None, reasoning, None
                    if delta.content:
                        text = delta.content
                        if t_first is None:
                            t_first = time.monotonic() - t0
                        parts.append(text)
                        yield text, None, None

        if aborted:
            # Control has left the `with`, so the client is closed and the
            # upstream generation already aborted; only now do the DB work.
            # Keyed off the break rather than the flag itself: a client that
            # disappears *after* a complete stream still has usage in hand and
            # must be billed normally, not written off as an abort.
            _abort()
            return

        duration = time.monotonic() - t0
        reply = "".join(parts)
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        reasoning_tokens = (
            getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            or getattr(usage, "reasoning_tokens", None)
        ) if usage else None

        cost = round(input_tokens * mc_in_cost / 1_000_000 + output_tokens * mc_out_cost / 1_000_000, 6)
        output_speed = output_tokens / duration if duration > 0 else 0.0

        if entity_id is not None:
            phase = "billing"
            with app.app_context():
                subtract_coins(entity_id, mc_id, cost, effective=effective)
                update_stats(
                    entity_id, mc_id, source,
                    input_tokens, output_tokens, cost,
                    endpoint_id=ep_id, duration=duration,
                    timing=timing, upstream_t0=t0,
                    ttft=t_first_any, ttft_visible=t_first, outcome="ok",
                )
                db.session.commit()
            billed = True
    except GeneratorExit:
        # Client disconnected mid-stream before billing — bill what was consumed
        # and log it as an abort, then re-raise to close cleanly.
        _abort()
        raise
    except Exception:
        # An upstream failure (including the read timeout above) ends the stream
        # just as surely as a disconnect, and is counted so the two are
        # distinguishable on the same metric. No billing here: the exception
        # propagates to the view, which owns the client-facing error.
        #
        # `phase` keeps a failed DB commit out of the upstream bucket: the
        # stream succeeded and only the accounting broke, and reporting that as
        # an upstream failure sends an operator hunting a backend problem that
        # does not exist.
        observe_stream_abort(source, f"{phase}_error")
        raise

    yield None, None, {
        "reply": reply,
        "model": remote_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking": "".join(thinking_parts) if thinking_parts else None,
        "thinking_tokens": reasoning_tokens,
        "cost": cost,
        "duration": duration,
        "time_to_first_token": t_first or duration,
        "output_speed": output_speed,
    }
