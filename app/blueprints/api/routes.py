import json
from datetime import datetime
from functools import wraps

import openai
from flask import Blueprint, request, jsonify, g, Response, stream_with_context

from app.extensions import db
from app.models.api_key import APIKey
from app.models.entity import Entity
from app.models.model_config import ModelConfig
from app.services.cost import calculate_cost
from app.services.llm import check_and_deduct_tokens, deduct_tokens, get_next_endpoint, update_stats

api_bp = Blueprint("api", __name__, url_prefix="/v1")


def _err(msg: str, err_type: str = "invalid_request_error", status: int = 400):
    return jsonify({"error": {"message": msg, "type": err_type}}), status


def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _err("Missing or malformed Authorization header", status=400)

        token = auth_header[7:].strip()
        if not token:
            return _err("Empty token", status=400)

        api_key = APIKey.query.filter_by(key=token).first()
        if not api_key or not api_key.active:
            return _err("Invalid or inactive API key", "authentication_error", 401)

        entity = Entity.query.get(api_key.entity_id)
        if not entity or not entity.active:
            return _err("Account disabled", "authentication_error", 403)

        g.api_key = api_key
        g.entity = entity
        return f(*args, **kwargs)

    return decorated


@api_bp.route("/models", methods=["GET"])
@api_key_required
def list_models():
    configs = ModelConfig.query.filter_by(active=True).all()
    data = [
        {
            "id": c.model_name,
            "object": "model",
            "created": int(c.created_at.timestamp()) if c.created_at else 0,
            "owned_by": "illm",
        }
        for c in configs
    ]
    return jsonify({"object": "list", "data": data})


@api_bp.route("/models/<model_id>", methods=["GET"])
@api_key_required
def get_model(model_id):
    config = ModelConfig.query.filter_by(model_name=model_id, active=True).first()
    if not config:
        return _err(f"Model '{model_id}' not found", status=404)
    return jsonify(
        {
            "id": config.model_name,
            "object": "model",
            "created": int(config.created_at.timestamp()) if config.created_at else 0,
            "owned_by": "illm",
        }
    )


def _do_chat(model_name: str, messages: list, stream: bool):
    """Shared logic for chat completions (used by both endpoints)."""
    model_config = ModelConfig.query.filter_by(model_name=model_name, active=True).first()
    if not model_config:
        return _err(f"Model '{model_name}' not found", status=404)

    entity_id = g.entity.id

    ok, code, msg = check_and_deduct_tokens(entity_id, model_config.id)
    if not ok:
        return _err(msg, status=code)

    endpoint = get_next_endpoint(model_config.id)
    if endpoint is None:
        return _err(f"No healthy endpoints for model '{model_name}'", "server_error", 503)

    client = openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url)
    remote_model = endpoint.model_name or model_name

    if stream:
        def generate():
            try:
                resp_stream = client.chat.completions.create(
                    model=remote_model, messages=messages, stream=True
                )
                for chunk in resp_stream:
                    yield f"data: {json.dumps(chunk.model_dump())}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return Response(stream_with_context(generate()), content_type="text/event-stream")

    try:
        response = client.chat.completions.create(model=remote_model, messages=messages)
    except Exception as e:
        return _err(str(e), "api_error", 500)

    usage = response.usage
    cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, model_config)

    deduct_tokens(entity_id, model_config.id, usage.prompt_tokens + usage.completion_tokens)
    update_stats(entity_id, model_config.id, "api", usage.prompt_tokens, usage.completion_tokens, cost)

    api_key = g.api_key
    api_key.input_tokens += usage.prompt_tokens
    api_key.output_tokens += usage.completion_tokens
    api_key.cost = float(api_key.cost) + cost
    api_key.last_used_at = datetime.utcnow()
    db.session.commit()

    return jsonify(response.model_dump())


@api_bp.route("/chat/completions", methods=["POST"])
@api_key_required
def chat_completions():
    data = request.get_json()
    if not data:
        return _err("Invalid request body")

    model_name = data.get("model")
    messages = data.get("messages", [])
    stream = data.get("stream", False)

    if not model_name or not messages:
        return _err("model and messages are required")

    return _do_chat(model_name, messages, stream)


@api_bp.route("/completions", methods=["POST"])
@api_key_required
def completions():
    data = request.get_json()
    if not data:
        return _err("Invalid request body")

    model_name = data.get("model")
    prompt = data.get("prompt", "")

    if not model_name or not prompt:
        return _err("model and prompt are required")

    messages = [{"role": "user", "content": prompt}]

    model_config = ModelConfig.query.filter_by(model_name=model_name, active=True).first()
    if not model_config:
        return _err(f"Model '{model_name}' not found", status=404)

    entity_id = g.entity.id
    ok, code, msg = check_and_deduct_tokens(entity_id, model_config.id)
    if not ok:
        return _err(msg, status=code)

    endpoint = get_next_endpoint(model_config.id)
    if endpoint is None:
        return _err(f"No healthy endpoints for model '{model_name}'", "server_error", 503)

    client = openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.url)
    remote_model = endpoint.model_name or model_name
    try:
        response = client.chat.completions.create(model=remote_model, messages=messages)
    except Exception as e:
        return _err(str(e), "api_error", 500)

    usage = response.usage
    cost = calculate_cost(usage.prompt_tokens, usage.completion_tokens, model_config)

    deduct_tokens(entity_id, model_config.id, usage.prompt_tokens + usage.completion_tokens)
    update_stats(entity_id, model_config.id, "api", usage.prompt_tokens, usage.completion_tokens, cost)

    api_key = g.api_key
    api_key.input_tokens += usage.prompt_tokens
    api_key.output_tokens += usage.completion_tokens
    api_key.cost = float(api_key.cost) + cost
    api_key.last_used_at = datetime.utcnow()
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
