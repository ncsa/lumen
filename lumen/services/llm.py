import logging
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from typing import NamedTuple

import openai
from flask import current_app
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

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


def check_coin_budget(entity_id: int, model_config_id: int, require_consent: bool = True):
    """Check coin budget. Returns (ok, http_code, error_message, effective).

    ``effective`` is the resolved coin pool limit (or None); pass it to subtract_coins
    afterward to avoid re-resolving model access and the pool limit per request.

    This is an optimistic gate: it checks that the balance is > 0 before the LLM
    call, but the actual cost is unknown until the call completes. A user with a tiny
    positive balance will pass this check, consume tokens, and have their balance
    zeroed by subtract_coins afterward. This is intentional — the budget is a soft
    spending limit, not a hard reservation.
    """
    effective = get_effective_limit(entity_id, model_config_id, require_consent=require_consent)
    if effective is None:
        return False, HTTPStatus.FORBIDDEN, "No access to this model", None
    max_coins, _, _starting = effective
    if max_coins == -2:
        return True, None, None, effective
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if balance is not None and float(balance.coins_left) <= 0:
        return False, HTTPStatus.TOO_MANY_REQUESTS, "Coin budget exhausted", None
    return True, None, None, effective


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
):
    """Update or create ModelStat/EntityStat running totals and append a RequestLog row."""
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
    )
    db.session.add(log)
    db.session.flush()


def record_aborted_request(entity_id, model_config_id, source, endpoint_id=None, duration=0.0):
    """Log a zero-cost request_logs row for a stream the client abandoned mid-response.

    Lets us monitor how often clients disconnect mid-stream. Tokens and cost are 0
    because the upstream usage totals only arrive in the final chunk, which we never
    received; since every hosted model has a coin cost, ``cost = 0`` identifies these
    aborted requests. Best-effort: never raise into the (already closing) generator.
    """
    try:
        db.session.add(RequestLog(
            time=datetime.now(timezone.utc),
            entity_id=entity_id,
            model_config_id=model_config_id,
            model_endpoint_id=endpoint_id,
            source=source,
            input_tokens=0,
            output_tokens=0,
            cost=0,
            duration=duration,
        ))
        db.session.commit()
    except Exception:
        logger.exception("failed to record aborted request (entity_id=%s, model=%s)", entity_id, model_config_id)
        db.session.rollback()


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
    """
    app = current_app._get_current_object()
    return _send_message_stream(app, messages, model, entity_id, source, effective)


def _send_message_stream(app, messages, model, entity_id, source, effective):
    with app.app_context():
        config = db.session.execute(select(ModelConfig).where(ModelConfig.model_name == model, ModelConfig.active)).scalar_one_or_none()
        if config is None:
            raise ValueError(f"Unknown or inactive model: {model}")

        endpoint = get_next_endpoint(config.id)
        if endpoint is None:
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

    t0 = time.time()
    t_first = None
    parts = []
    usage = None
    billed = False

    try:
        with openai.OpenAI(api_key=ep_api_key, base_url=ep_url) as client:
            stream = client.chat.completions.create(
                model=remote_model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                extra_body={"cache_salt": cache_salt} if cache_salt else {},
            )

            thinking_parts = []
            for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                    if reasoning:
                        thinking_parts.append(reasoning)
                        yield None, reasoning, None
                    if delta.content:
                        text = delta.content
                        if t_first is None:
                            t_first = time.time() - t0
                        parts.append(text)
                        yield text, None, None

        duration = time.time() - t0
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
            with app.app_context():
                subtract_coins(entity_id, mc_id, cost, effective=effective)
                update_stats(
                    entity_id, mc_id, source,
                    input_tokens, output_tokens, cost,
                    endpoint_id=ep_id, duration=duration,
                )
                db.session.commit()
            billed = True
    except GeneratorExit:
        # Client disconnected mid-stream before billing — log a zero-cost request
        # so we can monitor how often this happens, then re-raise to close cleanly.
        if entity_id is not None and not billed:
            with app.app_context():
                record_aborted_request(entity_id, mc_id, source, endpoint_id=ep_id, duration=time.time() - t0)
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
