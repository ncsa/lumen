import threading
import time
from datetime import datetime

import openai

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_limit import GroupLimit
from lumen.models.group_model_access import GroupModelAccess
from lumen.services.cost import calculate_cost

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


def get_model_access(entity_id: int, model_config_id: int) -> bool:
    """
    Return True if entity can access the given model, False otherwise.

    Resolution:
    1. User-level EntityModelAccess overrides everything.
    2. If any active group grants access (allowed=True), return True.
    3. No config = blocked (False).
    """
    user_access = EntityModelAccess.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()
    if user_access is not None:
        return user_access.allowed

    group_ids = _get_active_group_ids(entity_id)
    if group_ids:
        group_access = GroupModelAccess.query.filter(
            GroupModelAccess.group_id.in_(group_ids),
            GroupModelAccess.model_config_id == model_config_id,
            GroupModelAccess.allowed == True,  # noqa: E712
        ).first()
        if group_access is not None:
            return True

    return False


def get_pool_limit(entity_id: int):
    """
    Return (max_tokens, refresh_tokens, starting_tokens) for entity's token pool, or None if blocked.

    max_tokens == -2 means unlimited.

    Resolution: take the best limit from user's EntityLimit and their active group GroupLimits.
    User EntityLimit with max_tokens == 0 blocks regardless of groups.
    -2 (unlimited) wins over any positive value.
    """
    user_limit = EntityLimit.query.filter_by(entity_id=entity_id).first()
    if user_limit is not None and user_limit.max_tokens == 0:
        return None  # explicitly blocked

    group_ids = _get_active_group_ids(entity_id)
    group_limits = GroupLimit.query.filter(
        GroupLimit.group_id.in_(group_ids)
    ).all() if group_ids else []

    candidates = []
    if user_limit is not None and user_limit.max_tokens != 0:
        candidates.append((user_limit.max_tokens, user_limit.refresh_tokens, user_limit.starting_tokens))
    for gl in group_limits:
        if gl.max_tokens != 0:
            candidates.append((gl.max_tokens, gl.refresh_tokens, gl.starting_tokens))

    if not candidates:
        return None

    # -2 (unlimited) wins; otherwise take highest max_tokens
    for c in candidates:
        if c[0] == -2:
            return (-2, 0, 0)
    return max(candidates, key=lambda x: x[0])


def get_effective_limit(entity_id: int, model_config_id: int):
    """
    Return (max_tokens, refresh_tokens, starting_tokens) or None if blocked/no access.

    Checks model access first, then returns the entity's token pool.
    max_tokens == -2 means unlimited.
    """
    if not get_model_access(entity_id, model_config_id):
        return None
    return get_pool_limit(entity_id)


def get_token_balance(entity_id: int, model_config_id: int):
    """Return tokens_left for entity's pool, or None if unlimited or blocked."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return None
    max_tokens, _, starting = effective
    if max_tokens == -2:
        return None

    balance = EntityBalance.query.filter_by(entity_id=entity_id).first()
    if balance is None:
        balance = EntityBalance(
            entity_id=entity_id,
            tokens_left=starting,
        )
        db.session.add(balance)
        db.session.flush()

    return balance.tokens_left


def subtract_tokens(entity_id: int, model_config_id: int, tokens_used: int):
    """Deduct tokens_used from the entity's pool balance (no-op for unlimited or blocked)."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return
    max_tokens, _refresh, _starting = effective
    if max_tokens == -2:
        return

    balance = EntityBalance.query.filter_by(entity_id=entity_id).first()
    if balance:
        balance.tokens_left = balance.tokens_left - tokens_used
        db.session.flush()


def check_and_deduct_tokens(entity_id: int, model_config_id: int):
    """Check token budget. Returns (ok, http_code, error_message)."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return False, 403, "No access to this model"
    if effective[0] == -2:
        return True, None, None
    tokens_left = get_token_balance(entity_id, model_config_id)
    if tokens_left is not None and tokens_left <= 0:
        return False, 429, "Token budget exhausted"
    return True, None, None


def deduct_tokens(entity_id: int, model_config_id: int, tokens_used: int):
    """Deduct actual tokens used from the budget (no-op for unlimited or blocked)."""
    subtract_tokens(entity_id, model_config_id, tokens_used)


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


def send_message(
    messages: list,
    model: str,
    entity_id: int = None,
    api_key_obj=None,
    source: str = "chat",
) -> dict:
    """Send messages to LLM via round-robin, update stats, return response dict."""
    config = ModelConfig.query.filter_by(model_name=model, active=True).first()
    if config is None:
        raise ValueError(f"Unknown or inactive model: {model}")

    endpoint = get_next_endpoint(config.id)
    if endpoint is None:
        raise RuntimeError(f"No healthy endpoints for model '{model}'")

    client = openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url)
    remote_model = endpoint.model_name or model
    t0 = time.time()
    response = client.chat.completions.create(model=remote_model, messages=messages)
    duration = time.time() - t0

    usage = response.usage
    cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, config)
    output_speed = usage.completion_tokens / duration if duration > 0 else 0.0

    if entity_id is not None:
        deduct_tokens(entity_id, config.id, usage.prompt_tokens + usage.completion_tokens)
        update_stats(entity_id, config.id, source, usage.prompt_tokens, usage.completion_tokens, cost,
                     endpoint_id=endpoint.id, duration=duration)

    if api_key_obj is not None:
        api_key_obj.input_tokens += usage.prompt_tokens
        api_key_obj.output_tokens += usage.completion_tokens
        api_key_obj.cost = float(api_key_obj.cost) + cost
        api_key_obj.last_used_at = datetime.utcnow()
        db.session.flush()

    db.session.commit()

    return {
        "reply": response.choices[0].message.content,
        "model": response.model,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost": cost,
        "duration": duration,
        "time_to_first_token": duration,
        "output_speed": output_speed,
    }
