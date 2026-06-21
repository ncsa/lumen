import base64
import io
import json
import logging
from http import HTTPStatus

import filetype
import pypdf
from flask import Blueprint, Response, current_app, jsonify, render_template, request, session, stream_with_context
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

from sqlalchemy import and_, func, or_, select

from lumen.decorators import login_required
from lumen.extensions import db, limiter
from lumen.timeutils import utcnow
from lumen.models.conversation import Conversation
from lumen.models.message import Message
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.services.llm import bulk_model_access_info, check_coin_budget, get_pool_limit, send_message_stream

chat_bp = Blueprint("chat", __name__)

_DEFAULT_ALLOWED_EXTENSIONS = {
    "txt", "md", "csv", "json", "py", "js", "ts", "html", "css", "xml", "yaml", "yml",
    "pdf", "png", "jpg", "jpeg", "gif",
}
_DEFAULT_MAX_UPLOAD_MB = 10
_DEFAULT_MAX_TEXT_CHARS = 100_000

# Expected MIME types for binary document formats (filetype must agree).
_BINARY_DOC_MIMES = {"pdf": "application/pdf"}


def _upload_config():
    cfg = current_app.config.get("YAML_DATA", {}).get("chat", {}).get("upload", {})
    allowed = set(cfg.get("allowed_extensions", None) or _DEFAULT_ALLOWED_EXTENSIONS)
    max_bytes = int(cfg.get("max_size_mb", _DEFAULT_MAX_UPLOAD_MB)) * 1024 * 1024
    max_chars = int(cfg.get("max_text_chars", _DEFAULT_MAX_TEXT_CHARS))
    return allowed, max_bytes, max_chars


def _message_content_to_text(content):
    """Flatten OpenAI content (string or list) to a plain string for DB storage."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block["text"])
        elif block.get("type") == "image_url":
            parts.append("[Image: attached]")
    return "\n".join(parts)


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
    all_models = db.session.execute(select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)).scalars().all()
    healthy_counts = dict(
        db.session.execute(
            select(ModelEndpoint.model_config_id, func.count())
            .where(ModelEndpoint.healthy == True)  # noqa: E712
            .group_by(ModelEndpoint.model_config_id)
        ).all()
    )

    model_ids = [m.id for m in all_models]
    # Bulk-resolve access and consents to avoid N+1 per-model DB queries
    access_statuses, consent_map = bulk_model_access_info(entity_id, model_ids)
    default_ack = current_app.config.get("MODEL_DEFAULTS", {}).get("ack_message")
    # Pool limit is entity-level; fetch once rather than once per model via get_effective_limit
    pool = get_pool_limit(entity_id)

    # Include models that are accessible (not blocked) and have healthy endpoints.
    # Models that require acknowledgement without consent are shown with a warning so the
    # user can navigate to the model detail page to acknowledge them.
    available_models = []
    for m in all_models:
        if healthy_counts.get(m.id, 0) == 0:
            continue
        status = access_statuses.get(m.id, "allowed")
        if status == "blocked":
            continue
        if pool is None and status != "needs_ack":
            continue
        consented = (m.id in consent_map) if status == "needs_ack" else True
        consent_at = consent_map.get(m.id) if status == "needs_ack" else None
        notice = (m.ack_message or default_ack) if status == "needs_ack" else None
        available_models.append({"model": m, "status": status, "consented": consented, "consent_at": consent_at, "notice": notice})

    return render_template("chat.html", available_models=available_models)



@chat_bp.route("/chat/upload", methods=["POST"])
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def chat_upload():
    allowed, max_bytes, max_chars = _upload_config()

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), HTTPStatus.BAD_REQUEST

    safe_name = secure_filename(f.filename)
    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if ext not in allowed:
        return jsonify({"error": f"Unsupported file type: .{ext}"}), HTTPStatus.BAD_REQUEST

    data = f.read()
    if len(data) > max_bytes:
        return jsonify({"error": f"File exceeds {max_bytes // (1024 * 1024)} MB limit"}), HTTPStatus.BAD_REQUEST

    kind = filetype.guess(data)

    # ── Images ───────────────────────────────────────────────────────
    if kind is not None and kind.mime.startswith("image/"):
        data_url = f"data:{kind.mime};base64,{base64.b64encode(data).decode()}"
        return jsonify({"type": "image", "filename": safe_name, "data_url": data_url})

    # ── Documents ────────────────────────────────────────────────────
    if kind is not None:
        # Binary file that isn't an image — confirm it's a known doc format.
        expected_mime = _BINARY_DOC_MIMES.get(ext)
        if kind.mime != expected_mime:
            return jsonify({"error": "File content does not match its extension"}), HTTPStatus.BAD_REQUEST
    # kind is None → plain text (no magic bytes); expected for txt, csv, py, etc.

    if ext == "pdf":
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            logger.exception("PDF parse error: %s", safe_name)
            return jsonify({"error": "Could not read PDF"}), HTTPStatus.BAD_REQUEST
    else:
        text = data.decode("utf-8", errors="replace")

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[Document truncated at {max_chars:,} characters]"

    return jsonify({"type": "doc", "filename": safe_name, "text": text})


@chat_bp.route("/chat/stream", methods=["POST"])
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def chat_stream():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), HTTPStatus.BAD_REQUEST

    messages = data.get("messages", [])
    model = data.get("model")
    conversation_id = data.get("conversation_id")

    if not messages or not model:
        return jsonify({"error": "Missing messages or model"}), HTTPStatus.BAD_REQUEST

    _MAX_MESSAGES = 500
    _MAX_CHARS = 500_000
    if len(messages) > _MAX_MESSAGES:
        return jsonify({"error": f"Too many messages (max {_MAX_MESSAGES})"}), HTTPStatus.BAD_REQUEST
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if total_chars > _MAX_CHARS:
        return jsonify({"error": f"Message payload too large (max {_MAX_CHARS:,} characters)"}), HTTPStatus.BAD_REQUEST

    entity_id = session["entity_id"]

    model_config = db.session.execute(select(ModelConfig).where(ModelConfig.model_name == model, ModelConfig.active)).scalar_one_or_none()
    if not model_config:
        return jsonify({"error": f"Unknown model: {model}"}), HTTPStatus.BAD_REQUEST

    ok, code, msg, effective = check_coin_budget(entity_id, model_config.id)
    if not ok:
        return jsonify({"error": msg}), code

    def generate():
        try:
            result = None
            for chunk, thinking, final in send_message_stream(messages, model, entity_id=entity_id, source="chat", effective=effective):
                if thinking is not None:
                    yield f"data: {json.dumps({'thinking_chunk': thinking})}\n\n"
                elif chunk is not None:
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                else:
                    result = final

            if result is None:
                yield f"data: {json.dumps({'error': 'Empty response from model'})}\n\n"
                return

            conv = None
            if conversation_id:
                conv = db.session.execute(
                    select(Conversation).filter_by(id=conversation_id, entity_id=entity_id)
                ).scalar_one_or_none()

            if conv is None:
                user_msg = next((m for m in reversed(messages) if m["role"] == "user"), None)
                raw_content = _message_content_to_text(user_msg["content"]) if user_msg else ""
                title = raw_content[:40] if raw_content else "New Chat"
                conv = Conversation(entity_id=entity_id, title=title, model=model)
                db.session.add(conv)
                db.session.flush()

            user_msg = next((m for m in reversed(messages) if m["role"] == "user"), None)
            if user_msg:
                db.session.add(Message(
                    conversation_id=conv.id, role="user",
                    content=_message_content_to_text(user_msg["content"])
                ))

            db.session.add(Message(
                conversation_id=conv.id,
                role="assistant",
                content=result["reply"],
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                thinking=result.get("thinking"),
                thinking_tokens=result.get("thinking_tokens"),
                time_to_first_token=result.get("time_to_first_token"),
                duration=result.get("duration"),
                output_speed=result.get("output_speed"),
            ))

            conv.updated_at = utcnow()
            db.session.commit()

            result["conversation_id"] = conv.id
            result["done"] = True
            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            logger.exception("chat_stream error (model=%s, entity=%s)", model, entity_id)
            db.session.rollback()
            yield f"data: {json.dumps({'error': 'An error occurred. Please try again.'})}\n\n"

    resp = Response(stream_with_context(generate()), content_type="text/event-stream")
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@chat_bp.route("/chat/conversations")
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def list_conversations():
    entity_id = session["entity_id"]
    limit = min(request.args.get("limit", 50, type=int), 200)
    before_id = request.args.get("before", type=int)

    stmt = (
        select(Conversation)
        .filter_by(entity_id=entity_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    )
    if before_id is not None:
        anchor = db.session.execute(
            select(Conversation).filter_by(id=before_id, entity_id=entity_id)
        ).scalar_one_or_none()
        if anchor and anchor.updated_at is not None:
            stmt = stmt.where(
                or_(
                    Conversation.updated_at < anchor.updated_at,
                    and_(
                        Conversation.updated_at == anchor.updated_at,
                        Conversation.id < anchor.id,
                    ),
                )
            )

    convs = db.session.execute(stmt.limit(limit + 1)).scalars().all()
    has_more = len(convs) > limit
    convs = convs[:limit]
    conv_ids = [c.id for c in convs]
    last_msgs: dict[int, Message] = {}
    if conv_ids:
        subq = (
            select(Message.conversation_id, func.max(Message.created_at).label("max_at"))
            .group_by(Message.conversation_id)
            .subquery()
        )
        for msg in db.session.execute(
            select(Message).join(
                subq,
                (Message.conversation_id == subq.c.conversation_id)
                & (Message.created_at == subq.c.max_at)
            ).where(Message.conversation_id.in_(conv_ids))
        ).scalars():
            last_msgs[msg.conversation_id] = msg

    result = []
    for conv in convs:
        last_msg = last_msgs.get(conv.id)
        result.append({
            "id": conv.id,
            "title": conv.title,
            "model": conv.model,
            "updated_at": conv.updated_at.strftime('%Y-%m-%dT%H:%M:%SZ') if conv.updated_at else None,
            "last_message_preview": last_msg.content[:60] if last_msg else "",
        })
    return jsonify({"conversations": result, "has_more": has_more})


@chat_bp.route("/chat/conversations/<int:cid>/messages")
@login_required
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def get_conversation_messages(cid):
    entity_id = session["entity_id"]
    conv = db.session.execute(
        select(Conversation).filter_by(id=cid, entity_id=entity_id)
    ).scalar_one_or_none()
    if not conv:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    msgs = db.session.execute(
        select(Message).filter_by(conversation_id=cid).order_by(Message.created_at)
    ).scalars().all()
    result = []
    for msg in msgs:
        m = {
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.strftime('%Y-%m-%dT%H:%M:%SZ') if msg.created_at else None,
        }
        if msg.role == "assistant":
            m["meta"] = {
                "model": conv.model,
                "input_tokens": msg.input_tokens,
                "output_tokens": msg.output_tokens,
                "thinking": msg.thinking,
                "thinking_tokens": msg.thinking_tokens,
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
    conv = db.session.execute(select(Conversation).filter_by(id=cid, entity_id=entity_id)).scalar_one_or_none()
    if not conv:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    db.session.delete(conv)
    db.session.commit()
    return jsonify({"ok": True})
