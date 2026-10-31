import logging
import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from typing import NamedTuple, Optional

import openai
from flask import current_app, has_request_context, request
from sqlalchemy import case, literal, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from lumen.blueprints.metrics.middleware import observe_stream_abort
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.services.cost import calculate_cost
from lumen.services.crypto import cache_salt_for_entity
from lumen.services.model_resolver import resolve_model_config
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
#: which is what makes the next refill instant derivable here.
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
    model_needs_ack: bool = False,
    model_disabled: bool = False,
    model_early_access: bool = False,
    model_end_date=None,
) -> str:
    """Resolve 'allowed', 'needs_ack', or 'blocked' from pre-fetched per-model access data.

    - 'disabled' models and models past their end_date short-circuit to 'blocked'
      and cannot be overridden.
    - A model without an owner (owner_entity_id is None) is visible to everyone;
      an owned model is visible only to its owner.
    - 'needs_ack' means the model has acknowledgement requirements (needs_ack
      and/or early_access); these are model-level properties.
    """
    if model_disabled or (model_end_date is not None and model_end_date <= utcnow()):
        return "blocked"
    if owner_entity_id is not None and not is_owner:
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


def _model_baseline(model_config_ids: list) -> dict:
    """Load the access-relevant ModelConfig columns for ``model_config_ids``, keyed by id."""
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
    return baseline


def _access_statuses(entity_id: int, baseline: dict) -> dict:
    return {
        mc_id: _resolve_single_access(
            mc.owner_entity_id == entity_id,
            mc.owner_entity_id,
            model_needs_ack=mc.needs_ack,
            model_disabled=mc.disabled,
            model_early_access=mc.early_access,
            model_end_date=mc.end_date,
        )
        for mc_id, mc in baseline.items()
    }


def bulk_model_access_statuses(entity_id: int, model_config_ids: list) -> dict:
    """
    Return {model_config_id: "allowed"|"blocked"|"needs_ack"} for one entity.

    The access half of bulk_model_access_info, for callers that read consent
    rows themselves (see consent_rows) and so don't need the consent query.
    """
    if not model_config_ids:
        return {}
    return _access_statuses(entity_id, _model_baseline(model_config_ids))


def consent_rows(entity_id: int, model_config_ids: list) -> dict:
    """Return {model_config_id: EntityModelConsent} for the entity's existing consent rows."""
    if not model_config_ids:
        return {}
    return {
        r.model_config_id: r
        for r in db.session.execute(
            select(EntityModelConsent).where(
                EntityModelConsent.entity_id == entity_id,
                EntityModelConsent.model_config_id.in_(model_config_ids),
            )
        ).scalars().all()
    }


def bulk_model_access_info(entity_id: int, model_config_ids: list) -> tuple:
    """
    Bulk-resolve model access status and consents for one entity across many models.

    Returns (access_statuses, consent_map) where access_statuses is a dict
    {model_config_id: "allowed"|"blocked"|"needs_ack"} and consent_map is a dict
    {model_config_id: acknowledged_at} for models where the entity has satisfied
    ALL of the model's acknowledgement requirements (needs_ack and early_access);
    the value is the most recent acknowledgement timestamp. A model without an
    owner is public; an owned model is allowed only for its owner. Models with no
    acknowledgement requirement never appear in consent_map; callers only read it
    for a "needs_ack" status.

    Issues a fixed set of queries regardless of the number of models — replaces N+1
    per-model calls to get_model_access_status / has_model_consent.
    """
    if not model_config_ids:
        return {}, {}
    return _access_info(entity_id, _model_baseline(model_config_ids))


def _access_info(entity_id: int, baseline: dict) -> tuple:
    """bulk_model_access_info for an already-loaded baseline ({id: ModelConfig-like})."""
    # Only models with an acknowledgement requirement can need a consent row;
    # skip the query entirely when none of them has one.
    ack_ids = [mc_id for mc_id, mc in baseline.items() if mc.needs_ack or mc.early_access]

    # Only models whose acknowledgement requirements are ALL satisfied appear in
    # consent_map; a requirement added after consent leaves its timestamp NULL,
    # so the model drops out and the UI re-prompts.
    consent_map = {}
    for mc_id, row in consent_rows(entity_id, ack_ids).items():
        open_tags, acknowledged_at = consent_state(baseline[mc_id], row)
        if not open_tags and acknowledged_at is not None:
            consent_map[mc_id] = acknowledged_at

    return _access_statuses(entity_id, baseline), consent_map


def _model_access_info(entity_id: int, model_config_id: int, model_config=None) -> tuple:
    """bulk_model_access_info for one model, from the already-loaded ``model_config`` if given."""
    if model_config is None:
        return bulk_model_access_info(entity_id, [model_config_id])
    return _access_info(entity_id, {model_config_id: model_config})


_rr_counters: dict = {}
_rr_lock = threading.Lock()


def get_next_endpoint(model_config_id: int):
    """Return the next healthy endpoint for a model using round-robin, or None."""
    endpoints = db.session.execute(
        select(ModelEndpoint).filter_by(model_config_id=model_config_id, active=True, healthy=True).order_by(ModelEndpoint.id)
    ).scalars().all()
    if not endpoints:
        return None
    with _rr_lock:
        idx = _rr_counters.get(model_config_id, 0) % len(endpoints)
        _rr_counters[model_config_id] = idx + 1
    return endpoints[idx]


class StreamTarget(NamedTuple):
    """Plain scalars a streaming call needs from its model config and endpoint.

    Built while a session is current, so the generator that consumes it never
    touches an ORM object after the session that loaded it is gone.
    """
    remote_model: str
    url: str
    api_key: str
    endpoint_id: int
    model_config_id: int
    input_cost: float
    output_cost: float


def stream_target(config, endpoint) -> StreamTarget:
    """StreamTarget for ``config`` served by ``endpoint``.

    Forwards the endpoint's override name, else the CANONICAL model name —
    never an alias a client may have requested.
    """
    return StreamTarget(
        remote_model=endpoint.model_name or config.model_name,
        url=endpoint.url,
        api_key=endpoint.api_key,
        endpoint_id=endpoint.id,
        model_config_id=config.id,
        input_cost=float(config.input_cost_per_million),
        output_cost=float(config.output_cost_per_million),
    )


def get_model_access_status(entity_id: int, model_config_id: int) -> str:
    """
    Return 'allowed', 'blocked', or 'needs_ack' for the given entity + model.

    A model without an owner is allowed for everyone; an owned model is allowed
    only for its owner.
    """
    access_statuses, _ = bulk_model_access_info(entity_id, [model_config_id])
    return access_statuses[model_config_id]


def consent_state(mc, row) -> tuple:
    """Return (open_tags, acknowledged_at) for a model and an entity's consent row.

    ``mc`` is anything with ``needs_ack`` and ``early_access`` attributes (a
    ModelConfig or a column row); ``row`` is the entity's EntityModelConsent, or
    None. ``open_tags`` lists the requirements still unsatisfied
    (``"needs_ack"`` and/or ``"early_access"``). ``acknowledged_at`` is the most
    recent acknowledgement timestamp once none are open, else None. This is the
    single definition of "consent satisfied" behind both enforcement
    (get_model_access, check_coin_budget) and the consent state the UI and API
    report.
    """
    open_tags = []
    if mc.needs_ack and (row is None or row.consented_at is None):
        open_tags.append("needs_ack")
    if mc.early_access and (row is None or row.early_access_at is None):
        open_tags.append("early_access")
    if open_tags or row is None:
        return open_tags, None
    times = [t for t in (row.consented_at, row.early_access_at) if t is not None]
    return open_tags, max(times) if times else None


def _consent_satisfied(row, mc) -> bool:
    """True if a consent row covers all of a model's acknowledgement requirements."""
    if row is None:
        return False
    open_tags, _ = consent_state(mc, row)
    return not open_tags


def has_model_consent(entity_id: int, model_config_id: int) -> bool:
    """Return True if the entity has satisfied all of the model's acknowledgement requirements."""
    mc = db.session.get(ModelConfig, model_config_id)
    if mc is None:
        return False
    row = db.session.execute(
        select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=model_config_id)
    ).scalar_one_or_none()
    return _consent_satisfied(row, mc)


def model_notices(mc) -> tuple:
    """Return (ack_notice, early_access_notice) markdown strings for a ModelConfig.

    ack_notice is set iff the model has needs_ack (per-model message falling back
    to defaults.models.ack_message); early_access_notice is set iff the model is
    early_access (global defaults.models.early_access_message)."""
    defaults = current_app.config.get("MODEL_DEFAULTS", {})
    ack_notice = (mc.ack_message or defaults.get("ack_message")) if mc.needs_ack else None
    early_notice = defaults.get("early_access_message") if mc.early_access else None
    return ack_notice, early_notice


def ack_notice_text(mc):
    """Return the combined acknowledgement + early-access notice for a model.

    This is the text the web ack dialog shows, or None for a model with no
    notice text.
    """
    return "\n\n".join(n for n in model_notices(mc) if n) or None


def get_model_access(entity_id: int, model_config_id: int, model_config=None) -> bool:
    """
    Return True if entity can access the given model, False otherwise.

    For models that require acknowledgement (needs_ack and/or early_access), requires
    prior consent (EntityModelConsent) covering every current requirement. There is
    no way to skip this gate: chat and API requests are held to the same rule.

    ``model_config`` is the already-loaded ModelConfig (anything with its
    attributes); passing it skips reloading the model's access columns.
    """
    access_statuses, consent_map = _model_access_info(entity_id, model_config_id, model_config)
    status = access_statuses[model_config_id]
    if status == "blocked":
        return False
    if status == "needs_ack":
        return model_config_id in consent_map
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
    pool, _balance = _load_pool_state(entity_id)
    return pool


class PoolBalance(NamedTuple):
    coins_left: float
    last_refill_at: Optional[datetime]


def _load_pool_state(entity_id: int) -> tuple:
    """Return (PoolLimit | None, PoolBalance | None) for an entity in one statement.

    Loads the entity's own limit, its balance and the limits of its active
    groups together (one row per active group with a limit, or a single row
    without one), then applies get_pool_limit's rules. The balance is None when
    the entity has no balance row yet.
    """
    group_limits = (
        select(GroupMember.entity_id, GroupLimit.max_coins, GroupLimit.refresh_coins, GroupLimit.starting_coins)
        .join(Group, Group.id == GroupMember.group_id)
        .join(GroupLimit, GroupLimit.group_id == GroupMember.group_id)
        .where(GroupMember.entity_id == entity_id, Group.active == True)  # noqa: E712
        .subquery()
    )
    rows = db.session.execute(
        select(
            EntityLimit.id.label("limit_id"), EntityLimit.max_coins, EntityLimit.refresh_coins,
            EntityLimit.starting_coins,
            EntityBalance.id.label("balance_id"), EntityBalance.coins_left, EntityBalance.last_refill_at,
            group_limits.c.max_coins.label("group_max_coins"),
            group_limits.c.refresh_coins.label("group_refresh_coins"),
            group_limits.c.starting_coins.label("group_starting_coins"),
        )
        .select_from(Entity)
        .outerjoin(EntityLimit, EntityLimit.entity_id == Entity.id)
        .outerjoin(EntityBalance, EntityBalance.entity_id == Entity.id)
        .outerjoin(group_limits, group_limits.c.entity_id == Entity.id)
        .where(Entity.id == entity_id)
    ).all()
    first = rows[0] if rows else None

    balance = None
    if first is not None and first.balance_id is not None:
        balance = PoolBalance(float(first.coins_left), first.last_refill_at)

    if first is not None and first.limit_id is not None:
        if float(first.max_coins) == 0:
            return None, balance  # explicitly blocked
        return PoolLimit(float(first.max_coins), float(first.refresh_coins), float(first.starting_coins)), balance

    # No user-level limit — fall back to best group limit, then the global default pool.
    best = best_group_pool_limit(
        PoolLimit(r.group_max_coins, r.group_refresh_coins, r.group_starting_coins)
        for r in rows if r.group_max_coins is not None
    )
    if best is not None:
        return best, balance

    td = current_app.config.get("TOKEN_DEFAULTS")
    if td and float(td["max"]) != 0:
        return PoolLimit(float(td["max"]), float(td["refresh"]), float(td["starting"])), balance
    return None, balance


def entity_has_unlimited_pool(entity_id_column):
    """SQL expression matching ``get_pool_limit`` when its result is unlimited.

    Used by paginated tables so inherited unlimited pools sort and render
    correctly without resolving each row in Python.
    """
    own_limit_exists = select(EntityLimit.id).where(
        EntityLimit.entity_id == entity_id_column
    ).exists()
    own_unlimited_exists = select(EntityLimit.id).where(
        EntityLimit.entity_id == entity_id_column,
        EntityLimit.max_coins == -2,
    ).exists()
    active_group_limit = (
        select(GroupMember.id)
        .join(Group, Group.id == GroupMember.group_id)
        .join(GroupLimit, GroupLimit.group_id == GroupMember.group_id)
        .where(
            GroupMember.entity_id == entity_id_column,
            Group.active == True,  # noqa: E712
        )
    )
    unlimited_group_exists = active_group_limit.where(
        GroupLimit.max_coins == -2
    ).exists()
    usable_group_exists = active_group_limit.where(
        GroupLimit.max_coins != 0
    ).exists()

    defaults = current_app.config.get("TOKEN_DEFAULTS") or {}
    default_unlimited = float(defaults.get("max", 0)) == -2
    inherited_unlimited = unlimited_group_exists
    if default_unlimited:
        inherited_unlimited = inherited_unlimited | ~usable_group_exists
    return own_unlimited_exists | (~own_limit_exists & inherited_unlimited)


def get_effective_limit(entity_id: int, model_config_id: int, model_config=None):
    """
    Return (max_coins, refresh_coins, starting_coins) or None if blocked/no access.

    Checks model access (including acknowledgement) first, then returns the
    entity's coin pool.
    max_coins == -2 means unlimited. ``model_config`` is as for get_model_access.
    """
    if not get_model_access(entity_id, model_config_id, model_config):
        return None
    return get_pool_limit(entity_id)


def _bump(model, key: dict, increments: dict, insert_values: dict, last_used_at: datetime = None):
    """Apply ``increments`` to the ``model`` row matching ``key``, creating it on first use.

    The row exists on every call after an entity's first, so the atomic UPDATE
    runs alone: one statement, no existence SELECT. Only when it matches no row
    does the dialect ``INSERT … ON CONFLICT (key) DO UPDATE`` run, inserting
    ``key`` plus ``insert_values``; if a concurrent first request inserted the
    row in between, the conflict applies the same ``increments`` to it, so both
    requests are counted and nothing raises. Not an upsert every time: Postgres
    draws the serial ``id`` (model_stats, entity_balances) before it detects
    the conflict, so each steady-state call would burn a sequence value.

    ``increments`` reference the existing row through the model's columns,
    which ON CONFLICT DO UPDATE resolves against the conflicting row.

    ``last_used_at``, when given, moves the row's ``last_used_at`` forward to
    it but never back: the stats fold applies windows of past requests, so the
    time it carries can be older than the one already stored. A new row takes
    it as is, and ``increments`` must then not set ``last_used_at`` itself.
    """
    if last_used_at is not None:
        assert "last_used_at" not in increments, "pass last_used_at either way, not both"
        column = model.last_used_at
        increments = {**increments, "last_used_at": case(
            (or_(column.is_(None), column < last_used_at), literal(last_used_at, db.DateTime)),
            else_=column,
        )}
        insert_values = {**insert_values, "last_used_at": last_used_at}
    where = [getattr(model, col) == value for col, value in key.items()]
    result = db.session.execute(sa_update(model).where(*where).values(**increments))
    if result.rowcount:
        return
    dialect = postgresql if db.engine.dialect.name == "postgresql" else sqlite
    db.session.execute(
        dialect.insert(model)
        .values(**key, **insert_values)
        .on_conflict_do_update(index_elements=list(key), set_=increments)
    )


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

    # Single atomic deduction, floored at 0: deduct when affordable, otherwise zero
    # (the budget is a soft limit — see check_coin_budget). One statement so a
    # concurrent refill/credit can never be clobbered back to 0 by a separate zeroing.
    # The row is created on first use (first API use before login creates it),
    # already deducted from the starting balance.
    _bump(
        EntityBalance,
        {"entity_id": entity_id},
        {"coins_left": _greatest(0, EntityBalance.coins_left - coin_cost)},
        {"coins_left": max(0.0, float(starting) - float(coin_cost)), "last_refill_at": utcnow()},
    )
    db.session.flush()


def _denial_reason(pool, access_status: str) -> str:
    """Return the 403 reason for an entity denied access to a model.

    ``get_effective_limit`` collapses "blocked", "no usable coin pool" and
    "requires an acknowledgement nobody has given" into one None, and the
    taxonomy needs them apart: the last is the caller's to fix by
    acknowledging, the others are not. Pool eligibility is decided first: an
    entity with no usable pool is refused by the acknowledge endpoint no
    matter what it acknowledges, so labelling such a denial
    ``needs_consent`` would send the caller chasing a 404. Returns
    "needs_consent" or "no_access". Pure: ``pool`` and ``access_status`` are
    what check_coin_budget already loaded, and a denied "needs_ack" status
    means the consent is missing.
    """
    if pool is None:
        return "no_access"
    if access_status == "needs_ack":
        return "needs_consent"
    return "no_access"


class CoinBudget(NamedTuple):
    """Result of check_coin_budget."""
    ok: bool
    code: Optional[HTTPStatus]
    msg: Optional[str]
    effective: Optional[PoolLimit]
    reason: Optional[str]
    retry_after: Optional[int]


def check_coin_budget(entity_id: int, model_config_id: int,
                      source: str = None, model_name: str = "", model_config=None) -> CoinBudget:
    """Check coin budget. Returns a CoinBudget (ok, code, msg, effective, reason, retry_after).

    ``effective`` is the resolved coin pool limit (or None); pass it to subtract_coins
    afterward to avoid re-resolving model access and the pool limit per request.

    ``reason`` labels a failed check so the caller can disambiguate a 403
    (``"needs_consent"`` from ``"no_access"``) and a 429 (``"coin_budget"``); it
    is None on success. The web and API surfaces keep their own messages — the
    API uses the reason to emit a ``consent_required`` error code.

    ``retry_after`` is set only on a 429: the seconds until the balance is next
    credited (see coin_retry_after), or None when no refill is coming.

    ``model_config`` is the already-loaded ModelConfig (anything with its
    attributes); passing it skips reloading the model's access columns.

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
    access_statuses, consent_map = _model_access_info(entity_id, model_config_id, model_config)
    status = access_statuses[model_config_id]
    has_access = status == "allowed" or (status == "needs_ack" and model_config_id in consent_map)
    # A blocked model is "no_access" whatever the pool, so it is not loaded.
    pool, balance = _load_pool_state(entity_id) if status != "blocked" else (None, None)
    if not has_access or pool is None:
        reason = _denial_reason(pool, status)
        if source:
            observe_rejection_quietly(reason, source, model_name)
        return CoinBudget(False, HTTPStatus.FORBIDDEN, "No access to this model", None, reason, None)
    if pool.max_coins == -2:
        return CoinBudget(True, None, None, pool, None, None)
    if balance is not None and balance.coins_left <= 0:
        if source:
            observe_rejection_quietly("coin_budget", source, model_name)
        return CoinBudget(False, HTTPStatus.TOO_MANY_REQUESTS, "Coin budget exhausted", None, "coin_budget",
                          _retry_after(pool, balance))
    return CoinBudget(True, None, None, pool, None, None)


def _retry_after(pool, balance) -> Optional[int]:
    """Seconds until ``balance`` is next credited under ``pool``, or None. See coin_retry_after."""
    try:
        if pool is None:
            return None
        max_coins, refresh_coins, _starting = pool
        if max_coins == -2 or refresh_coins <= 0:
            return None
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
    check_coin_budget already returns this as ``retry_after`` on a 429.
    """
    try:
        pool, balance = _load_pool_state(entity_id)
    except Exception:  # noqa: BLE001 - a missing header beats a failed response
        logger.debug("could not derive Retry-After from the coin balance", exc_info=True)
        return None
    return _retry_after(pool, balance)


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
    """Append a RequestLog row, then update or create the ModelStat/EntityStat running totals.

    The RequestLog INSERT goes first and is flushed on its own: it takes no row
    lock another request waits on, so running it ahead of the counter UPDATEs
    keeps it out of their lock window. Billing should go through bill_request,
    which fixes the order of every write in the transaction.

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

    now = utcnow()

    _bump(
        ModelStat,
        {"entity_id": entity_id, "model_config_id": model_config_id, "source": source},
        {
            "requests": ModelStat.requests + 1,
            "input_tokens": ModelStat.input_tokens + input_tokens,
            "output_tokens": ModelStat.output_tokens + output_tokens,
            "audio_seconds": ModelStat.audio_seconds + audio_seconds,
            "cost": ModelStat.cost + cost,
            "last_used_at": now,
        },
        {
            "requests": 1, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "audio_seconds": audio_seconds, "cost": cost, "last_used_at": now,
        },
    )
    _bump(
        EntityStat,
        {"entity_id": entity_id},
        {
            "requests": EntityStat.requests + 1,
            "input_tokens": EntityStat.input_tokens + input_tokens,
            "output_tokens": EntityStat.output_tokens + output_tokens,
            "audio_seconds": EntityStat.audio_seconds + audio_seconds,
            "cost": EntityStat.cost + cost,
            "last_used_at": now,
        },
        {
            "requests": 1, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "audio_seconds": audio_seconds, "cost": cost, "last_used_at": now,
        },
    )


def _record_api_key_usage(api_key_id: int, input_tokens: int, output_tokens: int, cost: float, audio_seconds: int = 0):
    db.session.execute(
        sa_update(APIKey)
        .where(APIKey.id == api_key_id)
        .values(
            requests=APIKey.requests + 1,
            input_tokens=APIKey.input_tokens + input_tokens,
            output_tokens=APIKey.output_tokens + output_tokens,
            audio_seconds=APIKey.audio_seconds + audio_seconds,
            cost=APIKey.cost + Decimal(str(cost)),
            last_used_at=utcnow(),
        )
    )


def bill_request(entity_id: int, model_config_id: int, source: str, input_tokens: int, output_tokens: int,
                 cost: float, *, effective, api_key_id: int = None, audio_seconds: int = 0, **log_fields):
    """Bill one request in a single transaction, taking the hot row locks last, and commit.

    The only billing write path; every caller goes through it. The order is fixed:

    1. INSERT ``request_logs`` (flushed) — takes no row lock anyone waits on
    2. UPDATE ``model_stats``
    3. UPDATE ``entity_stats``
    4. UPDATE ``api_keys`` (when ``api_key_id`` is given)
    5. UPDATE ``entity_balances`` — the per-entity lock every request of the entity queues on
    6. COMMIT

    so the balance lock is held only for the commit, and because every billing
    transaction takes its locks in the same order, two of them can never deadlock.
    A new billing write must be added here, in this order, not beside a call.

    ``effective`` is the coin pool limit resolved during preflight, passed on to
    subtract_coins (``_UNSET`` re-resolves it there). ``log_fields`` are the
    remaining update_stats keywords (endpoint, timing, outcome, ...).
    """
    update_stats(entity_id, model_config_id, source, input_tokens, output_tokens, cost,
                 audio_seconds=audio_seconds, **log_fields)
    if api_key_id is not None:
        _record_api_key_usage(api_key_id, input_tokens, output_tokens, cost, audio_seconds=audio_seconds)
    subtract_coins(entity_id, model_config_id, cost, effective=effective)
    db.session.commit()


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
                           effective=_UNSET, api_key_id=None,
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

    ``api_key_id``, for an API request, also bills the key's running totals.

    Best-effort: never raise into the (already closing) generator.
    """
    try:
        bill_request(
            entity_id, model_config_id, source,
            input_tokens, output_tokens, cost,
            effective=effective, api_key_id=api_key_id,
            endpoint_id=endpoint_id, duration=duration, aborted=True,
            timing=timing, upstream_t0=upstream_t0,
            ttft=ttft, ttft_visible=ttft_visible, outcome="disconnect",
        )
    except Exception:
        logger.exception("failed to record aborted request (entity_id=%s, model=%s)", entity_id, model_config_id)
        db.session.rollback()


def record_stream_abort(app, *, billed, entity_id, model_config_id, source, endpoint_id, stream_t0,
                        input_tokens=0, output_tokens=0, cost=0.0, effective=_UNSET, api_key_id=None,
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
                effective=effective, api_key_id=api_key_id,
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
    model_config_id: int = None,
    resolved: StreamTarget = None,
):
    """Stream messages to LLM. Yields (chunk_text, None) for each token, then (None, result_dict).

    ``effective`` is the coin pool limit already resolved during preflight; it is
    threaded to subtract_coins to avoid re-resolving it after the stream completes.

    ``model_config_id`` carries the authorized canonical model already resolved
    during preflight. When set, it is used to load the config instead of
    re-resolving ``model``, so a config reload that retargets an alias after the
    view's access check can never route or bill this request against a different
    (possibly private) model. ``model`` is still used for display/error strings.

    ``resolved`` is the model and endpoint the view already picked, as a
    StreamTarget. When set, the generator does no model or endpoint lookup at
    all, so it checks out no DB connection before the first token. Without it,
    the generator resolves them itself (by ``model_config_id``, else by name)
    and re-checks there that the model is still active.

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
    return _send_message_stream(app, messages, model, entity_id, source, effective, disconnected, timing, model_config_id, resolved)


def _send_message_stream(app, messages, model, entity_id, source, effective, disconnected, timing, model_config_id,
                         resolved):
    with app.app_context():
        if resolved is None:
            if model_config_id is not None:
                config = db.session.get(ModelConfig, model_config_id)
                if config is None or not config.active:
                    raise ValueError(f"Unknown or inactive model: {model}")
            else:
                config = resolve_model_config(model)
                if config is None:
                    raise ValueError(f"Unknown or inactive model: {model}")

            endpoint = get_next_endpoint(config.id)
            if endpoint is None:
                observe_rejection_quietly("no_healthy_endpoint", source, model)
                raise RuntimeError(f"No healthy endpoints for model '{model}'")

            # Extract all scalars from ORM objects before the context exits. The
            # streaming LLM call can take minutes; the context teardown releases the
            # session (and its pool connection) before the first token is awaited.
            resolved = stream_target(config, endpoint)
        remote_model, ep_url, ep_api_key, ep_id, mc_id, mc_in_cost, mc_out_cost = resolved
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

        cost = calculate_cost(input_tokens, output_tokens, mc_in_cost, mc_out_cost)
        output_speed = output_tokens / duration if duration > 0 else 0.0

        if entity_id is not None:
            phase = "billing"
            with app.app_context():
                bill_request(
                    entity_id, mc_id, source,
                    input_tokens, output_tokens, cost,
                    effective=effective,
                    endpoint_id=ep_id, duration=duration,
                    timing=timing, upstream_t0=t0,
                    ttft=t_first_any, ttft_visible=t_first, outcome="ok",
                )
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
