import threading
import time
from datetime import datetime

import openai
from flask import current_app

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.global_model_access import GlobalModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_limit import GroupLimit
from lumen.models.group_model_access import GroupModelAccess
from lumen.services.cost import calculate_cost

# Priority order for access types when multiple apply (lower index = higher priority)
_ACCESS_PRIORITY = {"blacklist": 0, "graylist": 1, "whitelist": 2}

_rr_counters: dict = {}
_rr_lock = threading.Lock()


def get_next_endpoint(model_config_id: int):
    """Return the next healthy endpoint for a model using round-robin, or None."""
    endpoints = (
        ModelEndpoint.query.filter_by(model_config_id=model_config_id, healthy=True)
        .order_by(ModelEndpoint.id)
        .all()
    )
    if not endpoints:
        return None
    with _rr_lock:
        idx = _rr_counters.get(model_config_id, 0) % len(endpoints)
        _rr_counters[model_config_id] = idx + 1
    return endpoints[idx]


def _get_active_group_ids(entity_id: int) -> list:
    """Return list of active group IDs the entity belongs to."""
    return [
        m.group_id for m in (
            GroupMember.query
            .join(Group, Group.id == GroupMember.group_id)
            .filter(GroupMember.entity_id == entity_id, Group.active == True)  # noqa: E712
            .all()
        )
    ]


def get_model_access_status(entity_id: int, model_config_id: int) -> str:
    """
    Return 'allowed', 'blocked', or 'graylist' for the given entity + model.

    Resolution:
    1. User-level EntityModelAccess (allowed=True/False) overrides everything.
    2. Check active groups' GroupModelAccess: blacklist > whitelist > graylist.
    3. Check GlobalModelAccess: same priority.
    4. Apply effective default: most restrictive group model_access_default, then global MODEL_ACCESS_DEFAULT.
    """
    user_access = EntityModelAccess.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()
    if user_access is not None:
        return "allowed" if user_access.allowed else "blocked"

    # Global blacklist is absolute — no group can override it
    global_rule = GlobalModelAccess.query.filter_by(model_config_id=model_config_id).first()
    if global_rule is not None and global_rule.access_type == "blacklist":
        return "blocked"

    group_ids = _get_active_group_ids(entity_id)

    # Check per-model group rules (can override global graylist/whitelist)
    if group_ids:
        group_rules = GroupModelAccess.query.filter(
            GroupModelAccess.group_id.in_(group_ids),
            GroupModelAccess.model_config_id == model_config_id,
        ).all()
        if group_rules:
            best = min(group_rules, key=lambda r: _ACCESS_PRIORITY.get(r.access_type, 99))
            if best.access_type == "blacklist":
                return "blocked"
            if best.access_type == "whitelist":
                return "allowed"
            return "graylist"

    # Check remaining global per-model rules (graylist/whitelist)
    if global_rule is not None:
        if global_rule.access_type == "whitelist":
            return "allowed"
        return "graylist"

    # Apply effective default
    if group_ids:
        group_defaults = [
            g.model_access_default for g in
            Group.query.filter(Group.id.in_(group_ids), Group.model_access_default.isnot(None)).all()
        ]
        if group_defaults:
            # Most permissive wins: if any group allows, allow.
            # This matches individual model rule behavior (any whitelist grants access).
            if "whitelist" in group_defaults:
                return "allowed"
            if "graylist" in group_defaults:
                return "graylist"
            return "blocked"

    global_default = current_app.config.get("MODEL_ACCESS_DEFAULT", "whitelist")
    if global_default == "blacklist":
        return "blocked"
    if global_default == "graylist":
        return "graylist"
    return "allowed"


def has_model_consent(entity_id: int, model_config_id: int) -> bool:
    """Return True if the entity has consented to use a graylisted model."""
    return EntityModelConsent.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first() is not None


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

    Resolution: take the best limit from user's EntityLimit and their active group GroupLimits.
    User EntityLimit with max_coins == 0 blocks regardless of groups.
    -2 (unlimited) wins over any positive value.
    """
    user_limit = EntityLimit.query.filter_by(entity_id=entity_id).first()
    if user_limit is not None and float(user_limit.max_coins) == 0:
        return None  # explicitly blocked

    group_ids = _get_active_group_ids(entity_id)
    group_limits = GroupLimit.query.filter(
        GroupLimit.group_id.in_(group_ids)
    ).all() if group_ids else []

    candidates = []
    if user_limit is not None and float(user_limit.max_coins) != 0:
        candidates.append((float(user_limit.max_coins), float(user_limit.refresh_coins), float(user_limit.starting_coins)))
    for gl in group_limits:
        if float(gl.max_coins) != 0:
            candidates.append((float(gl.max_coins), float(gl.refresh_coins), float(gl.starting_coins)))

    if not candidates:
        return None

    # -2 (unlimited) wins; otherwise take highest max_coins
    for c in candidates:
        if c[0] == -2:
            return (-2, 0, 0)
    return max(candidates, key=lambda x: x[0])


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

    balance = EntityBalance.query.filter_by(entity_id=entity_id).first()
    if balance is None:
        balance = EntityBalance(
            entity_id=entity_id,
            coins_left=starting,
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

    balance = EntityBalance.query.filter_by(entity_id=entity_id).first()
    if balance:
        balance.coins_left = float(balance.coins_left) - coin_cost
        db.session.flush()


def check_coin_budget(entity_id: int, model_config_id: int):
    """Check coin budget. Returns (ok, http_code, error_message)."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return False, 403, "No access to this model"
    if effective[0] == -2:
        return True, None, None
    coins_left = get_coin_balance(entity_id, model_config_id)
    if coins_left is not None and coins_left <= 0:
        return False, 429, "Coin budget exhausted"
    return True, None, None


def deduct_coins(entity_id: int, model_config_id: int, coin_cost: float):
    """Deduct actual coin cost from the budget (no-op for unlimited or blocked)."""
    subtract_coins(entity_id, model_config_id, coin_cost)


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
    """Update or create ModelStat running totals and append a RequestLog row."""
    stat = ModelStat.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id, source=source
    ).first()
    if stat is None:
        stat = ModelStat(
            entity_id=entity_id,
            model_config_id=model_config_id,
            source=source,
            requests=0,
            input_tokens=0,
            output_tokens=0,
            cost=0,
        )
        db.session.add(stat)
    stat.requests += 1
    stat.input_tokens += input_tokens
    stat.output_tokens += output_tokens
    stat.cost = float(stat.cost) + cost
    stat.last_used_at = datetime.utcnow()

    log = RequestLog(
        time=datetime.utcnow(),
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
    config = ModelConfig.query.filter_by(model_name=model, active=True).first()
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
            reasoning = getattr(delta, "reasoning_content", None)
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

    cost = calculate_cost(input_tokens, output_tokens, config)
    output_speed = output_tokens / duration if duration > 0 else 0.0

    if entity_id is not None:
        deduct_coins(entity_id, config.id, cost)
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
        "cost": cost,
        "duration": duration,
        "time_to_first_token": t_first or duration,
        "output_speed": output_speed,
    }
