import logging
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from typing import NamedTuple

logger = logging.getLogger(__name__)

import openai
from flask import current_app
from sqlalchemy import select, update as sa_update
from sqlalchemy.exc import IntegrityError

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.entity_stat import EntityStat
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_limit import GroupLimit
from lumen.models.group_model_access import GroupModelAccess
from lumen.models.entity import Entity
from lumen.services.cost import calculate_cost

# Priority order for access types when multiple apply (lower index = higher priority)
_ACCESS_PRIORITY = {"blacklist": 0, "graylist": 1, "whitelist": 2}


class PoolLimit(NamedTuple):
    max_coins: float
    refresh_coins: float
    starting_coins: float


def bulk_model_access_info(entity_id: int, model_config_ids: list) -> tuple:
    """
    Bulk-resolve model access status and consents for one entity across many models.

    Returns (access_statuses, consented_ids) where access_statuses is a dict
    {model_config_id: "allowed"|"blocked"|"graylist"} and consented_ids is a set of
    model_config_ids where the entity has accepted the graylist notice.

    Issues a fixed set of queries regardless of the number of models — replaces N+1
    per-model calls to get_model_access_status / has_model_consent.
    """
    if not model_config_ids:
        return {}, set()

    ema_by_model = {
        r.model_config_id: r.access_type
        for r in db.session.execute(
            select(EntityModelAccess).where(
                EntityModelAccess.entity_id == entity_id,
                EntityModelAccess.model_config_id.in_(model_config_ids),
            )
        ).scalars().all()
    }

    group_ids = _get_active_group_ids(entity_id)

    gma_by_model: dict = {}
    group_defaults: list = []
    if group_ids:
        for r in db.session.execute(
            select(GroupModelAccess).where(
                GroupModelAccess.group_id.in_(group_ids),
                GroupModelAccess.model_config_id.in_(model_config_ids),
            )
        ).scalars().all():
            gma_by_model.setdefault(r.model_config_id, []).append(r.access_type)

        group_defaults = [
            g.model_access_default
            for g in db.session.execute(
                select(Group).where(Group.id.in_(group_ids), Group.model_access_default.isnot(None))
            ).scalars().all()
        ]

    entity = db.session.get(Entity, entity_id)
    entity_default = entity.model_access_default if entity else None

    consented_ids = {
        r.model_config_id
        for r in db.session.execute(
            select(EntityModelConsent).where(
                EntityModelConsent.entity_id == entity_id,
                EntityModelConsent.model_config_id.in_(model_config_ids),
            )
        ).scalars().all()
    }

    access_statuses: dict = {}
    for mc_id in model_config_ids:
        if mc_id in ema_by_model:
            t = ema_by_model[mc_id]
            access_statuses[mc_id] = "allowed" if t == "whitelist" else ("graylist" if t == "graylist" else "blocked")
        elif group_ids and mc_id in gma_by_model:
            best = min(gma_by_model[mc_id], key=lambda t: _ACCESS_PRIORITY.get(t, 99))
            access_statuses[mc_id] = "blocked" if best == "blacklist" else ("allowed" if best == "whitelist" else "graylist")
        elif group_defaults:
            if "whitelist" in group_defaults:
                access_statuses[mc_id] = "allowed"
            elif "graylist" in group_defaults:
                access_statuses[mc_id] = "graylist"
            else:
                access_statuses[mc_id] = "blocked"
        elif entity_default:
            access_statuses[mc_id] = "blocked" if entity_default == "blacklist" else ("graylist" if entity_default == "graylist" else "allowed")
        else:
            access_statuses[mc_id] = "allowed"

    return access_statuses, consented_ids


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
    Return 'allowed', 'blocked', or 'graylist' for the given entity + model.

    Resolution:
    1. User-level EntityModelAccess (whitelist/graylist/blacklist) overrides everything.
    2. Group per-model rules (GroupModelAccess): blacklist > whitelist > graylist.
    3. Effective default: most permissive group model_access_default wins.
    4. Entity-level default (for client entities).
    """
    user_access = db.session.execute(
        select(EntityModelAccess).filter_by(entity_id=entity_id, model_config_id=model_config_id)
    ).scalar_one_or_none()
    if user_access is not None:
        if user_access.access_type == "whitelist":
            return "allowed"
        if user_access.access_type == "graylist":
            return "graylist"
        return "blocked"

    group_ids = _get_active_group_ids(entity_id)

    if group_ids:
        group_rules = db.session.execute(
            select(GroupModelAccess).where(
                GroupModelAccess.group_id.in_(group_ids),
                GroupModelAccess.model_config_id == model_config_id,
            )
        ).scalars().all()
        if group_rules:
            best = min(group_rules, key=lambda r: _ACCESS_PRIORITY.get(r.access_type, 99))
            if best.access_type == "blacklist":
                return "blocked"
            if best.access_type == "whitelist":
                return "allowed"
            return "graylist"

    # Apply effective default
    if group_ids:
        group_defaults = [
            g.model_access_default for g in db.session.execute(
                select(Group).where(Group.id.in_(group_ids), Group.model_access_default.isnot(None))
            ).scalars().all()
        ]
        if group_defaults:
            # Most permissive wins: if any group allows, allow.
            # This matches individual model rule behavior (any whitelist grants access).
            if "whitelist" in group_defaults:
                return "allowed"
            if "graylist" in group_defaults:
                return "graylist"
            return "blocked"

    # Entity-level default (used for service/client entities configured via yaml)
    entity = db.session.get(Entity, entity_id)
    if entity and entity.model_access_default:
        if entity.model_access_default == "blacklist":
            return "blocked"
        if entity.model_access_default == "graylist":
            return "graylist"
        return "allowed"

    return "allowed"


def has_model_consent(entity_id: int, model_config_id: int) -> bool:
    """Return True if the entity has consented to use a graylisted model."""
    return db.session.execute(
        select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=model_config_id)
    ).scalar_one_or_none() is not None


def get_model_access(entity_id: int, model_config_id: int) -> bool:
    """
    Return True if entity can access the given model, False otherwise.

    For graylisted models, requires prior consent (EntityModelConsent).
    """
    status = get_model_access_status(entity_id, model_config_id)
    if status == "blocked":
        return False
    if status == "graylist":
        return has_model_consent(entity_id, model_config_id)
    return True


def get_pool_limit(entity_id: int):
    """
    Return (max_coins, refresh_coins, starting_coins) for entity's coin pool, or None if blocked.

    max_coins == -2 means unlimited.

    Resolution: user's EntityLimit always wins over group limits (consistent with model access).
    If no EntityLimit exists, fall back to the best GroupLimit (-2 beats any positive value).
    EntityLimit with max_coins == 0 blocks the entity regardless of groups.
    """
    user_limit = db.session.execute(select(EntityLimit).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if user_limit is not None:
        if float(user_limit.max_coins) == 0:
            return None  # explicitly blocked
        return PoolLimit(float(user_limit.max_coins), float(user_limit.refresh_coins), float(user_limit.starting_coins))

    # No user-level limit — fall back to best group limit.
    group_ids = _get_active_group_ids(entity_id)
    if not group_ids:
        return None
    group_limits = db.session.execute(
        select(GroupLimit).where(GroupLimit.group_id.in_(group_ids))
    ).scalars().all()
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


def get_effective_limit(entity_id: int, model_config_id: int):
    """
    Return (max_coins, refresh_coins, starting_coins) or None if blocked/no access.

    Checks model access first, then returns the entity's coin pool.
    max_coins == -2 means unlimited.
    """
    if not get_model_access(entity_id, model_config_id):
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
        balance = EntityBalance(
            entity_id=entity_id,
            coins_left=starting,
            last_refill_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.session.add(balance)
        db.session.flush()

    return float(balance.coins_left)


def subtract_coins(entity_id: int, model_config_id: int, coin_cost: float):
    """Deduct coin_cost from the entity's pool balance (no-op for unlimited or blocked)."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return
    max_coins, _refresh, _starting = effective
    if max_coins == -2:
        return

    result = db.session.execute(
        sa_update(EntityBalance)
        .where(EntityBalance.entity_id == entity_id, EntityBalance.coins_left >= coin_cost)
        .values(coins_left=EntityBalance.coins_left - coin_cost)
    )
    if result.rowcount == 0:
        logger.warning(
            "subtract_coins: balance exhausted for entity_id=%s (coin_cost=%.4f) — request was not charged",
            entity_id, coin_cost,
        )
    db.session.flush()


def check_coin_budget(entity_id: int, model_config_id: int):
    """Check coin budget. Returns (ok, http_code, error_message)."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return False, HTTPStatus.FORBIDDEN, "No access to this model"
    max_coins, _, _starting = effective
    if max_coins == -2:
        return True, None, None
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if balance is not None and float(balance.coins_left) <= 0:
        return False, HTTPStatus.TOO_MANY_REQUESTS, "Coin budget exhausted"
    return True, None, None


def update_stats(
    entity_id: int,
    model_config_id: int,
    source: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    endpoint_id: int = None,
    duration: float = 0.0,
):
    """Update or create ModelStat/EntityStat running totals and append a RequestLog row."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

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
            cost=EntityStat.cost + cost,
            last_used_at=now,
        )
    )

    log = RequestLog(
        time=datetime.now(timezone.utc).replace(tzinfo=None),
        entity_id=entity_id,
        model_config_id=model_config_id,
        model_endpoint_id=endpoint_id,
        source=source,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=cost,
        duration=duration,
    )
    db.session.add(log)
    db.session.flush()


def send_message_stream(
    messages: list,
    model: str,
    entity_id: int = None,
    source: str = "chat",
):
    """Stream messages to LLM. Yields (chunk_text, None) for each token, then (None, result_dict)."""
    config = db.session.execute(select(ModelConfig).filter_by(model_name=model, active=True)).scalar_one_or_none()
    if config is None:
        raise ValueError(f"Unknown or inactive model: {model}")

    endpoint = get_next_endpoint(config.id)
    if endpoint is None:
        raise RuntimeError(f"No healthy endpoints for model '{model}'")

    client = openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url)
    remote_model = endpoint.model_name or model
    t0 = time.time()
    t_first = None
    parts = []
    usage = None

    stream = client.chat.completions.create(
        model=remote_model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
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

    cost = calculate_cost(input_tokens, output_tokens, config)
    output_speed = output_tokens / duration if duration > 0 else 0.0

    if entity_id is not None:
        subtract_coins(entity_id, config.id, cost)
        update_stats(
            entity_id, config.id, source,
            input_tokens, output_tokens, cost,
            endpoint_id=endpoint.id, duration=duration,
        )
        db.session.commit()

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
