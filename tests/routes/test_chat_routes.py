"""Tests for chat routes (conversations, stream validation, access control)."""
from datetime import datetime, timezone
from http import HTTPStatus


def _grant_unlimited_pool(app, entity_id):
    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(
        entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0,
    ))
    db.session.commit()


def test_list_conversations_empty(auth_client):
    resp = auth_client.get("/chat/conversations")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["conversations"] == []


def test_list_conversations_with_data(app, auth_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        conv = Conversation(entity_id=test_user["id"], title="Test Chat", model="test-model")
        db.session.add(conv)
        db.session.commit()

    resp = auth_client.get("/chat/conversations")
    data = resp.get_json()
    assert resp.status_code == HTTPStatus.OK
    assert len(data["conversations"]) == 1
    assert data["conversations"][0]["title"] == "Test Chat"
    assert data["conversations"][0]["model"] == "test-model"


def test_get_conversation_messages_not_found(auth_client):
    resp = auth_client.get("/chat/conversations/9999/messages")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_get_conversation_messages(app, auth_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.message import Message
        conv = Conversation(entity_id=test_user["id"], title="Test", model="test-model")
        db.session.add(conv)
        db.session.flush()
        db.session.add(Message(conversation_id=conv.id, role="user", content="hello"))
        db.session.add(Message(
            conversation_id=conv.id, role="assistant", content="hi",
            input_tokens=5, output_tokens=3,
        ))
        db.session.commit()
        conv_id = conv.id

    resp = auth_client.get(f"/chat/conversations/{conv_id}/messages")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"
    assert "meta" in data["messages"][1]


def test_delete_conversation_not_found(auth_client):
    resp = auth_client.delete("/chat/conversations/9999")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_delete_conversation(app, auth_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.message import Message
        conv = Conversation(entity_id=test_user["id"], title="Gone", model="test-model")
        db.session.add(conv)
        db.session.flush()
        db.session.add(Message(conversation_id=conv.id, role="user", content="hello"))
        db.session.commit()
        conv_id = conv.id

    resp = auth_client.delete(f"/chat/conversations/{conv_id}")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["ok"] is True

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        assert db.session.get(Conversation, conv_id) is None


def test_chat_stream_no_body(auth_client):
    resp = auth_client.post("/chat/stream", content_type="application/json", data="")
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_stream_missing_model_and_messages(auth_client):
    resp = auth_client.post("/chat/stream", json={})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_stream_missing_model(auth_client):
    resp = auth_client.post("/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_stream_model_but_no_messages(auth_client, test_model):
    """model provided but messages list omitted → 400."""
    resp = auth_client.post("/chat/stream", json={"model": test_model["model_name"]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_stream_unknown_model(auth_client):
    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": "no-such-model",
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST


# ── Access control ────────────────────────────────────────────────────────────

def test_chat_stream_blacklisted_model_403(app, auth_client, test_user, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blocked",
        ))
        db.session.commit()

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_chat_stream_graylist_no_consent_403(app, auth_client, test_user, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.model_config import ModelConfig
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_chat_stream_graylist_with_consent_passes_access(
    app, auth_client, test_user, test_model,
):
    """needs_ack + consent clears the access gate (stream starts, fails at LLM level)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.models.model_config import ModelConfig
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.add(EntityModelConsent(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            consented_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code != HTTPStatus.FORBIDDEN


def test_chat_stream_holds_no_connection_at_yields(app, auth_client, test_user, test_model, monkeypatch):
    """The streaming generator must not hold a DB connection while suspended at
    a yield. If the client disconnects while an event is in flight, the
    generator is never closed, teardown never runs, and any connection checked
    out at that point stays checked out until the process restarts."""
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        from lumen.extensions import db
        pool = db.engine.pool

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        from lumen.extensions import db
        db.session.remove()  # the real send_message_stream does this before the LLM call
        yield "Hello", None, None
        yield None, None, {
            "reply": "Hello",
            "model": "test-model",
            "input_tokens": 1,
            "output_tokens": 1,
            "thinking": None,
            "thinking_tokens": None,
            "cost": 0.0,
            "duration": 0.1,
            "time_to_first_token": 0.05,
            "output_speed": 10.0,
        }

    from lumen.blueprints.chat import routes as chat_routes
    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.OK
    # Laziness is required: a buffered response would already have run teardown
    # and hidden any leak.
    assert resp.is_streamed

    saw_final = False
    try:
        for raw in resp.response:
            if b'"done": true' in raw:
                saw_final = True
                # The generator is suspended at its final yield right now.
                assert pool.checkedout() == 0, (
                    "DB connection checked out while the final SSE event is in "
                    "flight — a client disconnect here leaks it permanently"
                )
        assert saw_final
    finally:
        resp.close()


def test_chat_stream_error_path_holds_no_connection(app, auth_client, test_user, test_model, monkeypatch):
    """Same invariant for the except-path yield: after rollback, the generator
    must hold no connection while the error event is in flight."""
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        from lumen.extensions import db
        pool = db.engine.pool

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        from lumen.extensions import db
        db.session.remove()
        yield "Hello", None, None
        # Missing "reply" key → KeyError inside the billing block, after the
        # conversation SELECT/flush has checked out a connection.
        yield None, None, {
            "model": "test-model",
            "input_tokens": 1,
            "output_tokens": 1,
        }

    from lumen.blueprints.chat import routes as chat_routes
    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.OK
    assert resp.is_streamed

    saw_error = False
    try:
        for raw in resp.response:
            if b'"error"' in raw:
                saw_error = True
                # The generator is suspended at the except-path yield right now.
                assert pool.checkedout() == 0, (
                    "DB connection checked out while the error event is in flight"
                )
        assert saw_error
    finally:
        resp.close()


def test_chat_stream_whitelist_passes_access(app, auth_client, test_user, test_model):
    """Whitelist clears the access gate (stream starts, fails at LLM level)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code != HTTPStatus.FORBIDDEN
