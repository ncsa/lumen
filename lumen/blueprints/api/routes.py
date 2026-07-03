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
from lumen.timeutils import utcnow
from lumen.models.api_key import APIKey
from lumen.services.crypto import hash_api_key
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services.cost import calculate_audio_cost
from lumen.services.llm import bulk_model_access_info, check_coin_budget, subtract_coins, get_effective_limit, get_next_endpoint, get_pool_limit, update_stats, record_aborted_request

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


def _upstream_error_detail(exc):
    """Pull (message, type) from an upstream error body.

    Handles both the OpenAI shape ``{"error": {"message", "type"}}`` and the
    flat vLLM shape ``{"message", "type"}``.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message") or str(exc), err.get("type")
        if body.get("message"):
            return body["message"], body.get("type")
    return str(exc), None


def _classify_upstream_error(exc, context: str):
    """Log an upstream exception and map it to (message, type, HTTPStatus).

    A 4xx from the upstream is the caller's mistake (e.g. context length
    exceeded): pass the real status and message through so the caller can fix
    the request, and log it as a warning rather than an error. Everything else
    is a genuine upstream/transport failure and becomes a generic 500.
    """
    if isinstance(exc, openai.APIStatusError) and 400 <= exc.status_code < 500:
        msg, err_type = _upstream_error_detail(exc)
        logger.warning("%s (upstream %s): %s", context, exc.status_code, msg)
        return msg, err_type or "invalid_request_error", HTTPStatus(exc.status_code)
    logger.exception(context)
    return "Upstream error. Please try again.", "api_error", HTTPStatus.INTERNAL_SERVER_ERROR


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
        monitor_token = yaml_data.get("api", {}).get("monitoring", {}).get("token", "")
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
    configs = db.session.execute(select(ModelConfig).where(ModelConfig.active)).scalars().all()
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
        model_ids = [c.id for c in configs]
        access_statuses, consent_map = bulk_model_access_info(entity_id, model_ids)
        pool = get_pool_limit(entity_id)
        consent_required = current_app.config.get("API_REQUIRE_MODEL_CONSENT", True)
        data = [
            _model_dict(c, rates, eps_by_model.get(c.id, []))
            for c in configs
            if pool is not None
            and access_statuses.get(c.id, "allowed") != "blocked"
            and (
                not consent_required
                or access_statuses.get(c.id, "allowed") != "needs_ack"
                or c.id in consent_map
            )
        ]
    return jsonify({"object": "list", "data": data})


@api_bp.route("/models/<model_id>", methods=["GET"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def get_model(model_id):
    config = db.session.execute(select(ModelConfig).where(ModelConfig.model_name == model_id, ModelConfig.active)).scalar_one_or_none()
    if not config:
        return _err(f"Model '{model_id}' not found", status=HTTPStatus.NOT_FOUND)
    if not g.monitor:
        consent_required = current_app.config.get("API_REQUIRE_MODEL_CONSENT", True)
        if get_effective_limit(g.entity.id, config.id, require_consent=consent_required) is None:
            return _err(f"Model '{model_id}' not found", status=HTTPStatus.NOT_FOUND)
    eps = db.session.execute(
        select(ModelEndpoint).where(ModelEndpoint.model_config_id == config.id)
    ).scalars().all()
    rates = _get_request_rates()
    return jsonify(_model_dict(config, rates, list(eps)))


def _preflight(model_name: str):
    """Look up model, check coin budget, select endpoint.

    Returns (model_config, endpoint, effective, None) or (None, None, None, error_response).
    ``effective`` is the resolved coin pool limit, threaded to subtract_coins so the
    billing path does not re-resolve model access and the pool limit.
    """
    model_config = db.session.execute(select(ModelConfig).where(ModelConfig.model_name == model_name, ModelConfig.active)).scalar_one_or_none()
    if not model_config:
        return None, None, None, _err(f"Model '{model_name}' not found", status=HTTPStatus.NOT_FOUND)
    consent_required = current_app.config.get("API_REQUIRE_MODEL_CONSENT", True)
    ok, code, msg, effective = check_coin_budget(g.entity.id, model_config.id, require_consent=consent_required)
    if not ok:
        return None, None, None, _err(msg, status=code)
    endpoint = get_next_endpoint(model_config.id)
    if endpoint is None:
        return None, None, None, _err(f"No healthy endpoints for model '{model_name}'", "server_error", HTTPStatus.SERVICE_UNAVAILABLE)
    return model_config, endpoint, effective, None


def _complete_and_bill(model_name: str, messages: list, **kwargs):
    """Run a non-streaming upstream chat call and bill it.

    Shared by /v1/chat/completions (non-streaming) and /v1/completions. Returns
    (response, None) with the raw OpenAI response on success, or (None, error_response)
    on a preflight or upstream failure; the caller shapes the client-facing payload.
    """
    model_config, endpoint, effective, err = _preflight(model_name)
    if err:
        return None, err

    entity_id = g.entity.id
    ak_id = g.api_key.id
    remote_model = endpoint.model_name or model_name
    ep_api_key   = endpoint.api_key
    ep_url       = endpoint.url
    ep_id        = endpoint.id
    mc_id        = model_config.id
    mc_in_cost   = float(model_config.input_cost_per_million)
    mc_out_cost  = float(model_config.output_cost_per_million)
    db.session.remove()  # return connection to pool before the LLM call

    try:
        t0 = _time.time()
        with openai.OpenAI(api_key=ep_api_key, base_url=ep_url) as client:
            response = client.chat.completions.create(model=remote_model, messages=messages, **kwargs)
        duration = _time.time() - t0
    except Exception as exc:
        return None, _err(*_classify_upstream_error(
            exc, f"upstream LLM error (endpoint={ep_id} {ep_url} model={remote_model})"))

    usage = response.usage
    if usage is None:
        logger.warning("Upstream did not return usage data (model=%s, entity_id=%s)", model_name, entity_id)
        usage_prompt, usage_completion = 0, 0
    else:
        usage_prompt, usage_completion = usage.prompt_tokens, usage.completion_tokens

    cost = round(usage_prompt * mc_in_cost / 1_000_000 + usage_completion * mc_out_cost / 1_000_000, 6)
    subtract_coins(entity_id, mc_id, cost, effective=effective)
    update_stats(entity_id, mc_id, "api", usage_prompt, usage_completion, cost,
                 endpoint_id=ep_id, duration=duration)
    _record_api_key_usage(ak_id, usage_prompt, usage_completion, cost)
    db.session.commit()
    return response, None


def _merge_leading_system_messages(messages):
    """Collapse consecutive leading system messages into one.

    Some upstreams reject or ignore requests with more than one system message, so
    join any run of system messages at the start of the list into a single message.
    """
    if not messages or messages[0]["role"] != "system":
        return messages
    i = 0
    merged = []
    while i < len(messages) and messages[i]["role"] == "system":
        merged.append(messages[i]["content"])
        i += 1
    return [{"role": "system", "content": "\n\n".join(merged)}] + messages[i:]


def _do_chat(model_name: str, messages: list, stream: bool, **kwargs):
    """Shared logic for chat completions (used by both endpoints)."""
    messages = _merge_leading_system_messages(messages)
    if not stream:
        response, err = _complete_and_bill(model_name, messages, **kwargs)
        if err:
            return err
        return jsonify(response.model_dump())

    model_config, endpoint, effective, err = _preflight(model_name)
    if err:
        return err

    entity_id = g.entity.id
    ak_id = g.api_key.id

    # Extract all scalars from ORM objects before releasing the DB connection.
    # The streaming LLM call can take minutes — holding a pool connection for that
    # entire duration exhausts the pool under load.
    remote_model = endpoint.model_name or model_name
    ep_api_key   = endpoint.api_key
    ep_url       = endpoint.url
    ep_id        = endpoint.id
    mc_id        = model_config.id
    mc_in_cost   = float(model_config.input_cost_per_million)
    mc_out_cost  = float(model_config.output_cost_per_million)
    db.session.remove()  # return connection to pool before the LLM call

    def generate():
        billed = False
        with openai.OpenAI(api_key=ep_api_key, base_url=ep_url) as client:
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
                    cost = round(
                        usage.prompt_tokens * mc_in_cost / 1_000_000
                        + usage.completion_tokens * mc_out_cost / 1_000_000,
                        6,
                    )
                    subtract_coins(entity_id, mc_id, cost, effective=effective)
                    update_stats(entity_id, mc_id, "api", usage.prompt_tokens, usage.completion_tokens, cost,
                                 endpoint_id=ep_id)
                    _record_api_key_usage(ak_id, usage.prompt_tokens, usage.completion_tokens, cost)
                    db.session.commit()
                    billed = True
                else:
                    logger.warning(
                        "Upstream did not return usage data for streaming request "
                        "(model=%s, entity_id=%s) — tokens and cost not recorded.",
                        model_name, entity_id,
                    )
            except GeneratorExit:
                # Client disconnected mid-stream before billing — log a zero-cost
                # request so we can monitor how often this happens, then re-raise.
                if not billed:
                    record_aborted_request(entity_id, mc_id, "api", endpoint_id=ep_id)
                raise
            except Exception as exc:
                msg, err_type, _ = _classify_upstream_error(
                    exc,
                    f"Error during streaming request "
                    f"(endpoint={ep_id} {ep_url} model={remote_model}, entity_id={entity_id})",
                )
                yield f"data: {json.dumps({'error': {'message': msg, 'type': err_type}})}\n\n"
                yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream")


def _do_audio(kind: str):
    """Shared logic for /audio/transcriptions and /audio/translations.

    These endpoints are multipart/form-data (an audio file upload), not JSON.
    Billing branches on the upstream ``usage`` object: ASR backends report
    ``{type: "duration", seconds: N}`` and are billed per hour of audio, while
    gpt-4o-transcribe-style models report token usage and are billed per token.
    """
    model_name = request.form.get("model")
    upload = request.files.get("file")
    if not upload:
        return _err("file is required")
    if not model_name:
        return _err("model is required")

    # Read the upload into memory now, before we release the DB connection and
    # call upstream — the request stream is not available after db.session.remove().
    file_name = upload.filename or "audio"
    file_data = upload.read()
    file_type = upload.content_type or "application/octet-stream"

    # Optional pass-through params (multipart form values are strings).
    extra: dict = {}
    if kind == "transcriptions" and request.form.get("language"):
        extra["language"] = request.form["language"]
    if request.form.get("prompt"):
        extra["prompt"] = request.form["prompt"]
    if request.form.get("response_format"):
        extra["response_format"] = request.form["response_format"]
    if request.form.get("temperature"):
        try:
            extra["temperature"] = float(request.form["temperature"])
        except ValueError:
            return _err("temperature must be a number")

    model_config, endpoint, effective, err = _preflight(model_name)
    if err:
        return err

    entity_id = g.entity.id
    ak_id = g.api_key.id

    remote_model     = endpoint.model_name or model_name
    ep_api_key       = endpoint.api_key
    ep_url           = endpoint.url
    ep_id            = endpoint.id
    mc_id            = model_config.id
    mc_in_cost       = float(model_config.input_cost_per_million)
    mc_out_cost      = float(model_config.output_cost_per_million)
    mc_audio_per_hour = float(model_config.audio_cost_per_hour or 0)
    db.session.remove()

    try:
        t0 = _time.time()
        with openai.OpenAI(api_key=ep_api_key, base_url=ep_url) as client:
            create = getattr(client.audio, kind).create
            response = create(model=remote_model, file=(file_name, file_data, file_type), **extra)
        duration = _time.time() - t0
    except Exception as exc:
        return _err(*_classify_upstream_error(
            exc, f"upstream audio error (endpoint={ep_id} {ep_url} model={remote_model})"))

    # response is a pydantic model for json/verbose_json, or a plain string for
    # text/srt/vtt response formats (which carry no usage object).
    payload = response.model_dump() if hasattr(response, "model_dump") else {"text": str(response)}
    usage = payload.get("usage") if isinstance(payload, dict) else None

    in_tok, out_tok, seconds, cost = 0, 0, 0, 0.0
    if isinstance(usage, dict) and usage.get("type") == "duration":
        seconds = int(usage.get("seconds") or 0)
        cost = calculate_audio_cost(seconds, mc_audio_per_hour)
        if mc_audio_per_hour == 0:
            logger.warning(
                "Audio model has no audio_cost_per_hour set (model=%s, entity_id=%s) — billed as zero cost.",
                model_name, entity_id,
            )
    elif isinstance(usage, dict) and (usage.get("type") == "tokens" or "prompt_tokens" in usage):
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cost = round(in_tok * mc_in_cost / 1_000_000 + out_tok * mc_out_cost / 1_000_000, 6)
    else:
        logger.warning("Upstream did not return usage data (model=%s, entity_id=%s)", model_name, entity_id)

    subtract_coins(entity_id, mc_id, cost, effective=effective)
    update_stats(entity_id, mc_id, "api", in_tok, out_tok, cost,
                 endpoint_id=ep_id, duration=duration, audio_seconds=seconds)
    _record_api_key_usage(ak_id, in_tok, out_tok, cost, audio_seconds=seconds)
    db.session.commit()

    return jsonify(payload)


@api_bp.route("/audio/transcriptions", methods=["POST"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def audio_transcriptions():
    return _do_audio("transcriptions")


@api_bp.route("/audio/translations", methods=["POST"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def audio_translations():
    return _do_audio("translations")


@api_bp.route("/chat/completions", methods=["POST"])
@api_key_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def chat_completions():
    data = request.get_json(silent=True)
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
    data = request.get_json(silent=True)
    if not data:
        return _err("Invalid request body")

    model_name = data.get("model")
    prompt = data.get("prompt", "")

    if not model_name or not prompt:
        return _err("model and prompt are required")

    if data.get("stream", False):
        return _err("Streaming is not supported on /v1/completions; use /v1/chat/completions")

    response, err = _complete_and_bill(model_name, [{"role": "user", "content": prompt}])
    if err:
        return err
    if not response.choices:
        return _err("No response from model", "api_error", HTTPStatus.INTERNAL_SERVER_ERROR)

    usage = response.usage
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
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
            },
        }
    )
