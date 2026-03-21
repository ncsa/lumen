import threading
import time
from datetime import datetime

import openai

from lumen.extensions import db
from lumen.models.entity_model_balance import EntityModelBalance
from lumen.models.entity_model_limit import EntityModelLimit
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_model_limit import GroupModelLimit
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


def get_effective_limit(entity_id: int, model_config_id: int):
    """
    Compute effective (max_tokens, refresh_tokens, starting_tokens) for entity+model.

    Returns (max_tokens, refresh_tokens, starting_tokens) or None if blocked.
    max_tokens == -2 means unlimited.

    Resolution algorithm:
    1. Collect per-model entries (model_config_id = M) from user + all groups.
    2. Split into explicit (max_tokens != -1) vs defer (max_tokens == -1 or absent).
    3. If any explicit entries: positive wins over 0; all-0 => BLOCKED.
       Defaults (NULL rows) are NOT consulted when any explicit entry exists.
    4. If all defer => fall back to default rows (model_config_id = NULL).
    5. Apply same logic to defaults. No config => BLOCKED.
    """
    # Collect active group IDs for this entity
    group_ids = [
        m.group_id for m in (
            GroupMember.query
            .join(Group, Group.id == GroupMember.group_id)
            .filter(GroupMember.entity_id == entity_id, Group.active == True)  # noqa: E712
            .all()
        )
    ]

    def resolve(rows):
        """Given list of (max_tokens, refresh_tokens, starting_tokens), apply resolution."""
        if not rows:
            return "defer"
        explicit = [(mt, rt, st) for mt, rt, st in rows if mt != -1]
        if explicit:
            positives = [(mt, rt, st) for mt, rt, st in explicit if mt > 0 or mt == -2]
            if positives:
                # -2 (unlimited) wins everything; otherwise take max by max_tokens
                if any(mt == -2 for mt, rt, st in positives):
                    return (-2, 0, 0)
                best = max(positives, key=lambda x: x[0])
                return best
            # All explicit are 0 => BLOCKED
            return None
        # All defer (-1) => caller handles fallback to defaults
        return "defer"

    # Per-model rows from user
    user_row = EntityModelLimit.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()
    per_model_rows = []
    if user_row is not None:
        per_model_rows.append((user_row.max_tokens, user_row.refresh_tokens, user_row.starting_tokens))

    # Per-model rows from groups
    if group_ids:
        group_rows = GroupModelLimit.query.filter(
            GroupModelLimit.group_id.in_(group_ids),
            GroupModelLimit.model_config_id == model_config_id,
        ).all()
        for r in group_rows:
            per_model_rows.append((r.max_tokens, r.refresh_tokens, r.starting_tokens))

    result = resolve(per_model_rows)
    if result is None:
        return None  # BLOCKED
    if result != "defer":
        return result

    # All per-model entries defer (or none exist) => check default rows (model_config_id IS NULL)
    user_default = EntityModelLimit.query.filter_by(
        entity_id=entity_id, model_config_id=None
    ).first()
    default_rows = []
    if user_default is not None:
        default_rows.append((user_default.max_tokens, user_default.refresh_tokens, user_default.starting_tokens))

    if group_ids:
        group_defaults = GroupModelLimit.query.filter(
            GroupModelLimit.group_id.in_(group_ids),
            GroupModelLimit.model_config_id == None,  # noqa: E711
        ).all()
        for r in group_defaults:
            default_rows.append((r.max_tokens, r.refresh_tokens, r.starting_tokens))

    result = resolve(default_rows)
    if result is None or result == "defer":
        return None  # BLOCKED (no config or all defer with no defaults)
    return result


def _apply_refill(balance, max_tokens: int, refresh_tokens: int):
    """Apply lazy hourly refill to an EntityModelBalance row in-place."""
    now = datetime.utcnow()
    if refresh_tokens > 0 and balance.last_refill_at:
        hours_elapsed = (now - balance.last_refill_at).total_seconds() / 3600
        if hours_elapsed >= 1:
            refill = int(hours_elapsed) * refresh_tokens
            balance.tokens_left = min(max_tokens, balance.tokens_left + refill)
            balance.last_refill_at = now
            db.session.flush()


def get_token_balance(entity_id: int, model_config_id: int):
    """Return tokens_left for entity+model, or None if unlimited or blocked."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return None
    max_tokens, refresh_tokens, starting = effective
    if max_tokens == -2:
        return None

    balance = EntityModelBalance.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()
    if balance is None:
        balance = EntityModelBalance(
            entity_id=entity_id,
            model_config_id=model_config_id,
            tokens_left=starting,
        )
        db.session.add(balance)
        db.session.flush()

    _apply_refill(balance, max_tokens, refresh_tokens)
    return balance.tokens_left


def subtract_tokens(entity_id: int, model_config_id: int, tokens_used: int):
    """Deduct tokens_used from the balance row (no-op for unlimited or blocked)."""
    effective = get_effective_limit(entity_id, model_config_id)
    if effective is None:
        return
    max_tokens, _refresh, _starting = effective
    if max_tokens == -2:
        return

    balance = EntityModelBalance.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()
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
):
    """Update or create ModelStat running totals."""
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
        update_stats(entity_id, config.id, source, usage.prompt_tokens, usage.completion_tokens, cost)

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
