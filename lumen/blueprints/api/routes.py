import hmac
import json
import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from http import HTTPStatus

import openai
from flask import Blueprint, current_app, request, jsonify, g, Response, stream_with_context
from sqlalchemy import case, func, select, update as sa_update

logger = logging.getLogger(__name__)

from lumen.extensions import db, limiter
from lumen.models.api_key import APIKey
from lumen.services.crypto import hash_api_key
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services.cost import calculate_cost
from lumen.services.llm import check_coin_budget, subtract_coins, get_effective_limit, get_next_endpoint, update_stats

api_bp = Blueprint("api", __name__, url_prefix="/v1")

# Per-worker in-memory cache. Under multi-worker deployments each worker holds its own
# copy; effective cache lifetime is correct per-worker but not shared across workers.
_rates_cache: dict = {"data": {}, "expires_at": 0.0}
_rates_lock = threading.Lock()


def _api_key_id():
    api_key = getattr(g, "api_key", None)
    return str(api_key.id) if api_key else (request.remote_addr or "unknown")


def _api_limit():
    cfg = current_app.config.get("YAML_DATA", {})
    return cfg.get("rate_limiting", {}).get("limit", "30 per minute")


def _record_api_key_usage(api_key_id: int, input_tokens: int, output_tokens: int, cost: float):
    db.session.execute(
        sa_update(APIKey)
        .where(APIKey.id == api_key_id)
        .values(
            requests=APIKey.requests + 1,
            input_tokens=APIKey.input_tokens + input_tokens,
            output_tokens=APIKey.output_tokens + output_tokens,
            cost=APIKey.cost + Decimal(str(cost)),
            last_used_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )


def _get_request_rates() -> dict:
    """Return {model_config_id: {last_minute, last_hour, last_day}}, cached 30s per worker."""
    now = _time.monotonic()
    if now < _rates_cache["expires_at"]:
        return _rates_cache["data"]

    with _rates_lock:
        if now < _rates_cache["expires_at"]:
            return _rates_cache["data"]

        n = datetime.now(timezone.utc)
        minute_ago = n - timedelta(minutes=1)
        hour_ago = n - timedelta(hours=1)
        day_ago = n - timedelta(days=1)

        stmt = (
            select(
                RequestLog.model_config_id,
                func.count(case((RequestLog.time > minute_ago, 1))).label("last_minute"),
                func.count(case((RequestLog.time > hour_ago, 1))).label("last_hour"),
                func.count(case((RequestLog.time > day_ago, 1))).label("last_day"),
            )
            .where(RequestLog.time > day_ago)
            .where(RequestLog.model_config_id.isnot(None))
            .group_by(RequestLog.model_config_id)
        )
        result = {
            row.model_config_id: {
                "last_minute": row.last_minute,
                "last_hour": row.last_hour,
                "last_day": row.last_day,
            }
            for row in db.session.execute(stmt).all()
        }
        _rates_cache["data"] = result
        _rates_cache["expires_at"] = now + 30.0
    return result


def _model_dict(c, rates: dict, eps: list) -> dict:
    """Serialize a ModelConfig to an OpenAI/vLLM-compatible response dict."""
    zero = {"last_minute": 0, "last_hour": 0, "last_day": 0}
    healthy_count = sum(1 for e in eps if e.healthy)
    if not eps or healthy_count == 0:
        status = "down"
    elif healthy_count < len(eps):
        status = "degraded"
    else:
        status = "ok"

    # root is the upstream model identifier (e.g. "Qwen/Qwen3-Coder-Next-FP8")
    root = eps[0].model_name if eps and eps[0].model_name else c.model_name

    d = {
        "id": c.model_name,
        "object": "model",
        "created": int(c.created_at.timestamp()) if c.created_at else 0,
        "owned_by": "lumen",
        "root": root,
        "parent": None,
        # vLLM-compatible context window field
        "max_model_len": c.context_window,
        # Lumen status / cost fields
        "status": status,
        "input_cost_per_million": float(c.input_cost_per_million),
        "output_cost_per_million": float(c.output_cost_per_million),
        "instances": {
            "configured": len(eps),
            "healthy": healthy_count,
        },
        "requests": rates.get(c.id, zero),
    }

    # Capability fields — omitted when null so clients can detect "unknown" vs "false"
    for field in (
        "description",
        "max_output_tokens",
        "supports_function_calling",
        "supports_reasoning",
        "knowledge_cutoff",
        "input_modalities",
        "output_modalities",
    ):
        val = getattr(c, field, None)
        if val is not None:
            d[field] = val

    return d


def _err(msg: str, err_type: str = "invalid_request_error", status: HTTPStatus = HTTPStatus.BAD_REQUEST):
    return jsonify({"error": {"message": msg, "type": err_type}}), status


def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _err("Missing or malformed Authorization header", status=HTTPStatus.BAD_REQUEST)

        token = auth_header[7:].strip()
        if not token:
            return _err("Empty token", status=HTTPStatus.BAD_REQUEST)

        yaml_data = current_app.config.get("YAML_DATA", {})
        monitor_token = yaml_data.get("monitoring", {}).get("token", "")
        if monitor_token and hmac.compare_digest(token, monitor_token):
            if request.endpoint not in ("api.list_models", "api.get_model"):
                return _err("Monitor token can only access /v1/models", "authentication_error", HTTPStatus.FORBIDDEN)
            g.api_key = None
            g.entity = None
            g.monitor = True
            return f(*args, **kwargs)

        g.monitor = False
        api_key = db.session.execute(select(APIKey).filter_by(key_hash=hash_api_key(token))).scalar_one_or_none()
        if not api_key or not api_key.active:
            return _err("Invalid or inactive API key", "authentication_error", HTTPStatus.UNAUTHORIZED)

        entity = db.session.get(Entity, api_key.entity_id)
        if not entity or not entity.active:
            return _err("Account disabled", "authentication_error", HTTPStatus.FORBIDDEN)

        g.api_key = api_key
        g.entity = entity
        return f(*args, **kwargs)

    return decorated


@api_bp.route("/models", methods=["GET"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def list_models():
    configs = db.session.execute(select(ModelConfig).filter_by(active=True)).scalars().all()
    eps_by_model: dict = {}
    for ep in db.session.execute(
        select(ModelEndpoint).where(ModelEndpoint.model_config_id.in_([c.id for c in configs]))
    ).scalars().all():
        eps_by_model.setdefault(ep.model_config_id, []).append(ep)
    rates = _get_request_rates()
    if g.monitor:
        data = [_model_dict(c, rates, eps_by_model.get(c.id, [])) for c in configs]
    else:
        entity_id = g.entity.id
        data = [_model_dict(c, rates, eps_by_model.get(c.id, [])) for c in configs if get_effective_limit(entity_id, c.id) is not None]
    return jsonify({"object": "list", "data": data})


@api_bp.route("/models/<model_id>", methods=["GET"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def get_model(model_id):
    config = db.session.execute(select(ModelConfig).filter_by(model_name=model_id, active=True)).scalar_one_or_none()
    if not config:
        return _err(f"Model '{model_id}' not found", status=HTTPStatus.NOT_FOUND)
    if not g.monitor and get_effective_limit(g.entity.id, config.id) is None:
        return _err(f"Model '{model_id}' not found", status=HTTPStatus.NOT_FOUND)
    eps = db.session.execute(
        select(ModelEndpoint).where(ModelEndpoint.model_config_id == config.id)
    ).scalars().all()
    rates = _get_request_rates()
    return jsonify(_model_dict(config, rates, list(eps)))


def _preflight(model_name: str):
    """Look up model, check coin budget, select endpoint. Returns (model_config, endpoint, None) or (None, None, error_response)."""
    model_config = db.session.execute(select(ModelConfig).filter_by(model_name=model_name, active=True)).scalar_one_or_none()
    if not model_config:
        return None, None, _err(f"Model '{model_name}' not found", status=HTTPStatus.NOT_FOUND)
    ok, code, msg = check_coin_budget(g.entity.id, model_config.id)
    if not ok:
        return None, None, _err(msg, status=code)
    endpoint = get_next_endpoint(model_config.id)
    if endpoint is None:
        return None, None, _err(f"No healthy endpoints for model '{model_name}'", "server_error", HTTPStatus.SERVICE_UNAVAILABLE)
    return model_config, endpoint, None


def _do_chat(model_name: str, messages: list, stream: bool, **kwargs):
    """Shared logic for chat completions (used by both endpoints)."""
    model_config, endpoint, err = _preflight(model_name)
    if err:
        return err

    entity_id = g.entity.id

    remote_model = endpoint.model_name or model_name

    if stream:
        api_key = g.api_key

        def generate():
            with openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url) as client:
                try:
                    stream_options = {**kwargs.pop("stream_options", {}), "include_usage": True}
                    resp_stream = client.chat.completions.create(
                        model=remote_model, messages=messages, stream=True,
                        stream_options=stream_options,
                        **kwargs,
                    )
                    usage = None
                    for chunk in resp_stream:
                        if chunk.usage is not None:
                            usage = chunk.usage
                        yield f"data: {json.dumps(chunk.model_dump())}\n\n"
                    yield "data: [DONE]\n\n"

                    if usage is not None:
                        cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, model_config)
                        subtract_coins(entity_id, model_config.id, cost)
                        update_stats(entity_id, model_config.id, "api", usage.prompt_tokens, usage.completion_tokens, cost,
                                     endpoint_id=endpoint.id)
                        _record_api_key_usage(api_key.id, usage.prompt_tokens, usage.completion_tokens, cost)
                        db.session.commit()
                    else:
                        logger.warning(
                            "Upstream did not return usage data for streaming request "
                            "(model=%s, entity_id=%s) — tokens and cost not recorded.",
                            model_name, entity_id,
                        )
                except Exception as e:
                    logger.exception(
                        "Error during streaming request (model=%s, entity_id=%s)", model_name, entity_id
                    )
                    yield f"data: {json.dumps({'error': 'Upstream error. Please try again.'})}\n\n"

        return Response(stream_with_context(generate()), content_type="text/event-stream")

    try:
        t0 = _time.time()
        with openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url) as client:
            response = client.chat.completions.create(model=remote_model, messages=messages, **kwargs)
        duration = _time.time() - t0
    except Exception as e:
        return _err(str(e), "api_error", HTTPStatus.INTERNAL_SERVER_ERROR)

    usage = response.usage
    cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, model_config)

    subtract_coins(entity_id, model_config.id, cost)
    update_stats(entity_id, model_config.id, "api", usage.prompt_tokens, usage.completion_tokens, cost,
                 endpoint_id=endpoint.id, duration=duration)

    _record_api_key_usage(g.api_key.id, usage.prompt_tokens, usage.completion_tokens, cost)
    db.session.commit()

    return jsonify(response.model_dump())


@api_bp.route("/chat/completions", methods=["POST"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def chat_completions():
    data = request.get_json()
    if not data:
        return _err("Invalid request body")

    model_name = data.get("model")
    messages = data.get("messages", [])
    stream = data.get("stream", False)

    if not model_name or not messages:
        return _err("model and messages are required")

    extra = {k: v for k, v in data.items() if k not in ("model", "messages", "stream")}
    return _do_chat(model_name, messages, stream, **extra)


@api_bp.route("/completions", methods=["POST"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def completions():
    data = request.get_json()
    if not data:
        return _err("Invalid request body")

    model_name = data.get("model")
    prompt = data.get("prompt", "")

    if not model_name or not prompt:
        return _err("model and prompt are required")

    messages = [{"role": "user", "content": prompt}]

    model_config, endpoint, err = _preflight(model_name)
    if err:
        return err

    entity_id = g.entity.id
    remote_model = endpoint.model_name or model_name
    try:
        t0 = _time.time()
        with openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url) as client:
            response = client.chat.completions.create(model=remote_model, messages=messages)
        duration = _time.time() - t0
    except Exception as e:
        return _err(str(e), "api_error", HTTPStatus.INTERNAL_SERVER_ERROR)

    usage = response.usage
    cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, model_config)

    subtract_coins(entity_id, model_config.id, cost)
    update_stats(entity_id, model_config.id, "api", usage.prompt_tokens, usage.completion_tokens, cost,
                 endpoint_id=endpoint.id, duration=duration)

    _record_api_key_usage(g.api_key.id, usage.prompt_tokens, usage.completion_tokens, cost)
    db.session.commit()

    return jsonify(
        {
            "id": response.id,
            "object": "text_completion",
            "created": response.created,
            "model": response.model,
            "choices": [
                {
                    "text": response.choices[0].message.content,
                    "index": 0,
                    "finish_reason": response.choices[0].finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
    )
