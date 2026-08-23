import base64
import io
import json
import logging
from http import HTTPStatus

import filetype
import pypdf
from flask import Blueprint, Response, current_app, jsonify, render_template, request, session
from sqlalchemy import and_, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from lumen.decorators import login_required
from lumen.extensions import db, limiter
from lumen.models.conversation import Conversation
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.message import Message
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.services.live_state import get_live_state
from lumen.services.llm import bulk_model_access_info, check_coin_budget, coin_retry_after, get_pool_limit, model_notices, send_message_stream
from lumen.services.wsgi_disconnect import client_disconnect_event
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)

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


def _count_conversation_started(entity_id):
    """Bump the persistent conversation counter in entity_stats.

    The counter survives conversation deletion and disabled storage. Uses the
    same race-safe ensure-row + atomic-increment pattern as llm.py's stats.
    """
    if db.session.execute(select(EntityStat).filter_by(entity_id=entity_id)).scalar_one_or_none() is None:
        try:
            with db.session.begin_nested():
                db.session.add(EntityStat(entity_id=entity_id, requests=0, input_tokens=0, output_tokens=0, cost=0))
        except IntegrityError:
            pass
    db.session.execute(
        sa_update(EntityStat)
        .where(EntityStat.entity_id == entity_id)
        .values(conversations=EntityStat.conversations + 1)
    )


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
    # Pool limit is entity-level; fetch once rather than once per model via get_effective_limit
    pool = get_pool_limit(entity_id)

    # Include models that are accessible, funded, and have healthy endpoints.
    # Models that require acknowledgement remain visible only when the entity
    # has a coin pool and can use them after acknowledging.
    available_models = []
    for m in all_models:
        if healthy_counts.get(m.id, 0) == 0:
            continue
        status = access_statuses.get(m.id, "allowed")
        if status == "blocked":
            continue
        if pool is None:
            continue
        consented = (m.id in consent_map) if status == "needs_ack" else True
        consent_at = consent_map.get(m.id) if status == "needs_ack" else None
        notice, early_notice = model_notices(m) if status == "needs_ack" else (None, None)
        available_models.append({"model": m, "status": status, "consented": consented, "consent_at": consent_at,
                                 "notice": notice, "early_notice": early_notice})

    store_conversations = db.session.execute(
        select(Entity.store_conversations).where(Entity.id == entity_id)
    ).scalar_one()

    return render_template("chat.html", available_models=available_models, store_conversations=store_conversations)



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

    ok, code, msg, effective = check_coin_budget(
        entity_id, model_config.id, source="chat", model_name=model,
    )
    if not ok:
        if code == HTTPStatus.TOO_MANY_REQUESTS:
            # The chat surface has the same two-kinds-of-429 problem /v1 has:
            # the limiter's 429 already carries Retry-After, so without one
            # here an exhausted budget looks like a rate limit that will clear
            # in a moment. Only the header is added. The body stays
            # {"error": "<string>"} because chat.html renders `data.error`
            # directly (chat.html:710) -- nesting it the way /v1 does would
            # put "[object Object]" in the user's chat window.
            retry_after = coin_retry_after(entity_id)
            if retry_after is not None:
                return jsonify({"error": msg}), code, {"Retry-After": str(retry_after)}
        return jsonify({"error": msg}), code

    store_conversations = db.session.execute(
        select(Entity.store_conversations).where(Entity.id == entity_id)
    ).scalar_one()
    if not store_conversations:
        conversation_id = None
    # With storage disabled there is no conversation row to signal a new chat;
    # a payload with a single user message is the first exchange of one.
    is_new_conversation = sum(1 for m in messages if m.get("role") == "user") == 1

    # Release the connection checked out by the queries above — the streaming
    # loop below can run for many minutes with no further DB activity, and
    # holding a connection idle-in-transaction that long risks Postgres
    # killing it (idle_in_transaction_session_timeout), which then surfaces
    # as "server closed the connection unexpectedly" on a later, unrelated
    # request that reuses the now-dead pooled connection. A fresh connection
    # is opened lazily by generate() once it needs to write the conversation.
    db.session.remove()

    # The generator body runs after this request's contexts are gone, on
    # whatever worker thread iterates the response body. It must not depend on
    # ambient request/app context (stream_with_context re-pushes the request's
    # context onto that thread and poisons it if the generator is abandoned),
    # so: create the LLM stream while the request context is still current, and
    # push short-lived app contexts around the DB work — never across a yield.
    app = current_app._get_current_object()
    disconnected = client_disconnect_event()
    llm_stream = send_message_stream(messages, model, entity_id=entity_id, source="chat", effective=effective)
    # Admitted last, once every rejection above has passed: a refused request
    # must never appear in flight. The ticket rides in the closure like the
    # disconnect Event, and generate()'s finally releases it. Nothing between
    # here and the Response below can raise, so an admitted request always
    # reaches the generator. The backend is resolved here too — picking one
    # reads config, which the context-free generator cannot do.
    live_state = get_live_state()
    ticket = live_state.admit(model, entity_id)

    def generate():
        try:
            result = None
            for chunk, thinking, final in llm_stream:
                if final is not None:
                    # Taken before the disconnect check, not after: reaching the
                    # final tuple means the stream ran to completion and
                    # send_message_stream has already billed it (outcome "ok",
                    # aborted false). A client that leaves inside that billing
                    # window — a few milliseconds, but a busy one — would
                    # otherwise break here and lose the reply it paid for, with
                    # nothing in request_logs to say the conversation was
                    # dropped. Keep it and let the write below run.
                    result = final
                    continue
                if disconnected.is_set():
                    break
                if thinking is not None:
                    yield f"data: {json.dumps({'thinking_chunk': thinking})}\n\n"
                elif chunk is not None:
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            if result is None and disconnected.is_set():
                # The client left mid-stream; send_message_stream has already
                # recorded the abort. Return silently rather than falling into
                # the branch below: the model did not return empty, and on a
                # half-open connection that error event is delivered to a live
                # client that just received partial output.
                # Guarded on `result is None` so a client that disconnects after
                # a complete reply still reaches the conversation write — that
                # reply was generated and billed, so it must be saved.
                return

            if result is None:
                yield f"data: {json.dumps({'error': 'Empty response from model'})}\n\n"
                return

            if store_conversations:
                # The conversation write gets its own short-lived app context: its
                # teardown releases the session before the final yield below, so no
                # connection is held while the last event is in flight — if the
                # client disconnected, this generator may never be closed.
                with app.app_context():
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
                        _count_conversation_started(entity_id)

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
                    # Read conv.id before commit: expire_on_commit would otherwise
                    # check out a fresh connection to refresh it, and that connection
                    # would still be held during the final yield below.
                    conv_id = conv.id
                    db.session.commit()

                result["conversation_id"] = conv_id
            elif is_new_conversation:
                # Storage is off, but the lifetime conversation counter still
                # counts the chat that just started.
                with app.app_context():
                    _count_conversation_started(entity_id)
                    db.session.commit()
            result["done"] = True
            yield f"data: {json.dumps(result)}\n\n"

        except Exception:
            # Any half-done DB work was already rolled back when its app
            # context exited; there is no ambient session here to clean up.
            logger.exception("chat_stream error (model=%s, entity=%s)", model, entity_id)
            yield f"data: {json.dumps({'error': 'An error occurred. Please try again.'})}\n\n"
        finally:
            # Bound the live count before anything that could raise. close()
            # can itself raise (a generator that does not handle the
            # GeneratorExit thrown at its yield propagates RuntimeError), so it
            # must never run ahead of the release — a leak that only shows as an
            # inflated count. The ticket's deadline is the backstop, but the
            # release is the honest path.
            live_state.release(ticket)
            # Close the LLM stream here rather than leaving it to be collected.
            # Closing it is what raises GeneratorExit inside send_message_stream,
            # and that handler is where an abandoned stream gets billed. Two
            # exits above leave it suspended mid-stream: the disconnect check in
            # the loop, and a GeneratorExit thrown in at a yield. Under
            # refcounting the collection is usually immediate, but anything
            # still referencing this frame (a traceback, a log record carrying
            # exc_info) postpones it indefinitely and an exception raised during
            # collection is swallowed — so the abort accounting would silently
            # never happen. A no-op once the stream has run to completion.
            llm_stream.close()

    resp = Response(generate(), content_type="text/event-stream")
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
