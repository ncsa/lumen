import json
import logging
from datetime import datetime

from flask import Blueprint, Response, current_app, jsonify, render_template, request, session, stream_with_context

logger = logging.getLogger(__name__)

from lumen.decorators import login_required
from lumen.extensions import db, limiter
from lumen.models.conversation import Conversation
from lumen.models.message import Message
from lumen.models.model_config import ModelConfig
from lumen.services.llm import check_and_deduct_tokens, get_effective_limit, send_message_stream

chat_bp = Blueprint("chat", __name__)


def _chat_entity_id():
    entity_id = session.get("entity_id")
    return str(entity_id) if entity_id else (request.remote_addr or "unknown")


def _chat_limit():
    cfg = current_app.config.get("YAML_DATA", {})
    return cfg.get("rate_limiting", {}).get("limit", "30 per minute")


@chat_bp.route("/chat")
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def chat_page():
    entity_id = session["entity_id"]
    all_models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()
    active_models = [m for m in all_models if get_effective_limit(entity_id, m.id) is not None]
    return render_template("chat.html", active_models=active_models)



@chat_bp.route("/chat/stream", methods=["POST"])
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def chat_stream():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    messages = data.get("messages", [])
    model = data.get("model")
    conversation_id = data.get("conversation_id")

    if not messages or not model:
        return jsonify({"error": "Missing messages or model"}), 400

    entity_id = session["entity_id"]

    model_config = ModelConfig.query.filter_by(model_name=model, active=True).first()
    if not model_config:
        return jsonify({"error": f"Unknown model: {model}"}), 400

    ok, code, msg = check_and_deduct_tokens(entity_id, model_config.id)
    if not ok:
        return jsonify({"error": msg}), code

    def generate():
        try:
            result = None
            for chunk, final in send_message_stream(messages, model, entity_id=entity_id, source="chat"):
                if chunk is not None:
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                else:
                    result = final

            if result is None:
                yield f"data: {json.dumps({'error': 'Empty response from model'})}\n\n"
                return

            conv = None
            if conversation_id:
                conv = Conversation.query.filter_by(
                    id=conversation_id, entity_id=entity_id, hidden=False
                ).first()

            if conv is None:
                user_msg = next((m for m in reversed(messages) if m["role"] == "user"), None)
                title = user_msg["content"][:40] if user_msg else "New Chat"
                conv = Conversation(entity_id=entity_id, title=title, model=model)
                db.session.add(conv)
                db.session.flush()

            user_msg = next((m for m in reversed(messages) if m["role"] == "user"), None)
            if user_msg:
                db.session.add(Message(
                    conversation_id=conv.id, role="user", content=user_msg["content"]
                ))

            db.session.add(Message(
                conversation_id=conv.id,
                role="assistant",
                content=result["reply"],
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                time_to_first_token=result.get("time_to_first_token"),
                duration=result.get("duration"),
                output_speed=result.get("output_speed"),
            ))

            conv.updated_at = datetime.utcnow()
            db.session.commit()

            result["conversation_id"] = conv.id
            result["done"] = True
            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            logger.exception("chat_stream error (model=%s, entity=%s)", model, entity_id)
            db.session.rollback()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    resp = Response(stream_with_context(generate()), content_type="text/event-stream")
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@chat_bp.route("/chat/conversations")
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def list_conversations():
    entity_id = session["entity_id"]
    convs = (
        Conversation.query
        .filter_by(entity_id=entity_id, hidden=False)
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    result = []
    for conv in convs:
        last_msg = (
            Message.query
            .filter_by(conversation_id=conv.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        result.append({
            "id": conv.id,
            "title": conv.title,
            "model": conv.model,
            "updated_at": conv.updated_at.strftime('%Y-%m-%dT%H:%M:%S') if conv.updated_at else None,
            "last_message_preview": last_msg.content[:60] if last_msg else "",
        })
    return jsonify({"conversations": result})


@chat_bp.route("/chat/conversations/<int:cid>/messages")
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def get_conversation_messages(cid):
    entity_id = session["entity_id"]
    conv = Conversation.query.filter_by(
        id=cid, entity_id=entity_id, hidden=False
    ).first()
    if not conv:
        return jsonify({"error": "Not found"}), 404

    msgs = (
        Message.query.filter_by(conversation_id=cid).order_by(Message.created_at).all()
    )
    result = []
    for msg in msgs:
        m = {
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.strftime('%Y-%m-%dT%H:%M:%S') if msg.created_at else None,
        }
        if msg.role == "assistant":
            m["meta"] = {
                "input_tokens": msg.input_tokens,
                "output_tokens": msg.output_tokens,
                "time_to_first_token": msg.time_to_first_token,
                "duration": msg.duration,
                "output_speed": msg.output_speed,
            }
        result.append(m)
    return jsonify({"messages": result})


@chat_bp.route("/chat/conversations/<int:cid>", methods=["DELETE"])
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def delete_conversation(cid):
    entity_id = session["entity_id"]
    conv = Conversation.query.filter_by(id=cid, entity_id=entity_id).first()
    if not conv:
        return jsonify({"error": "Not found"}), 404

    mode = current_app.config.get("CHAT_CONVERSATION_REMOVE_MODE", "hide")
    if mode == "delete":
        db.session.delete(conv)
    else:
        conv.hidden = True
    db.session.commit()
    return jsonify({"ok": True})
