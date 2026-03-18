import threading
from datetime import datetime

import openai

from app.extensions import db
from app.models.model_config import ModelConfig
from app.models.model_endpoint import ModelEndpoint
from app.models.model_limit import ModelLimit
from app.models.model_stat import ModelStat
from app.services.cost import calculate_cost

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


def check_and_deduct_tokens(entity_id: int, model_config_id: int):
    """Check token budget. Returns (ok, http_code, error_message)."""
    limit = ModelLimit.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()

    if limit is None:
        return False, 403, "No token budget configured for this model"

    if limit.token_limit == -1:
        return False, 403, "No access to this model"

    if limit.token_limit == -2:
        return True, None, None

    # Lazy hourly refill
    now = datetime.utcnow()
    if limit.tokens_per_hour > 0 and limit.last_refill_at:
        hours_elapsed = (now - limit.last_refill_at).total_seconds() / 3600
        if hours_elapsed >= 1:
            refill = int(hours_elapsed) * limit.tokens_per_hour
            limit.tokens_left = min(limit.token_limit, limit.tokens_left + refill)
            limit.last_refill_at = now
            db.session.flush()

    if limit.tokens_left <= 0:
        return False, 429, "Token budget exhausted"

    return True, None, None


def deduct_tokens(entity_id: int, model_config_id: int, tokens_used: int):
    """Deduct actual tokens used from the budget (no-op for unlimited)."""
    limit = ModelLimit.query.filter_by(
        entity_id=entity_id, model_config_id=model_config_id
    ).first()
    if limit and limit.token_limit not in (-1, -2):
        limit.tokens_left = max(0, limit.tokens_left - tokens_used)
        db.session.flush()


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
    response = client.chat.completions.create(model=remote_model, messages=messages)

    usage = response.usage
    cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, config)

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
    }
