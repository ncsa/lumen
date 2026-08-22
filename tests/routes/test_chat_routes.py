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

def test_chat_stream_owned_model_403(app, auth_client, test_user, test_model):
    """A model owned by another user is blocked for the non-owner."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from tests.conftest import set_model_owner
        _grant_unlimited_pool(app, test_user["id"])
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_chat_stream_ack_no_consent_403(app, auth_client, test_user, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_chat_stream_ack_with_consent_passes_access(
    app, auth_client, test_user, test_model,
):
    """needs_ack + consent clears the access gate (stream starts, fails at LLM level).

    Asserted as OK rather than "not FORBIDDEN": the failure this test guards
    against is the access gate refusing a consented user, but "not 403" is also
    satisfied by a 500 from a view that crashed after the gate, which would
    certify the opposite of what the name claims.
    """
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.models.model_config import ModelConfig
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
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
    assert resp.status_code == HTTPStatus.OK
    resp.close()


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
        # The real send_message_stream runs context-free between yields; its
        # DB phases each push their own short-lived app context.
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


def test_chat_stream_skips_persistence_when_storing_disabled(app, auth_client, test_user, test_model, monkeypatch):
    """With store_conversations off, a stream persists nothing and the final
    event carries no conversation_id — even if the client sends one."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(Entity, test_user["id"]).store_conversations = False
        db.session.commit()

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
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
        "conversation_id": 12345,
    })
    assert resp.status_code == HTTPStatus.OK

    body = resp.get_data(as_text=True)
    assert '"done": true' in body
    assert "conversation_id" not in body

    with app.app_context():
        from sqlalchemy import func, select

        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.message import Message
        assert db.session.scalar(select(func.count(Conversation.id))) == 0
        assert db.session.scalar(select(func.count(Message.id))) == 0


def _fake_stream(messages, model, entity_id=None, source="chat", effective=None):
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


def _conversation_counter(app, entity_id):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        return db.session.scalar(
            select(EntityStat.conversations).filter_by(entity_id=entity_id)
        ) or 0


def test_chat_stream_counts_conversation_when_storing(app, auth_client, test_user, test_model, monkeypatch):
    """A new stored conversation bumps the persistent counter; continuing it does not."""
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])

    from lumen.blueprints.chat import routes as chat_routes
    monkeypatch.setattr(chat_routes, "send_message_stream", _fake_stream)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    body = resp.get_data(as_text=True)
    assert '"done": true' in body
    assert _conversation_counter(app, test_user["id"]) == 1

    import json as _json
    conv_id = _json.loads(body.strip().splitlines()[-1].removeprefix("data: "))["conversation_id"]

    resp = auth_client.post("/chat/stream", json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "more"},
        ],
        "model": test_model["model_name"],
        "conversation_id": conv_id,
    })
    assert '"done": true' in resp.get_data(as_text=True)
    assert _conversation_counter(app, test_user["id"]) == 1


def test_chat_stream_counts_conversation_when_storing_disabled(app, auth_client, test_user, test_model, monkeypatch):
    """With storage off, the first exchange bumps the counter; follow-ups don't."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(Entity, test_user["id"]).store_conversations = False
        db.session.commit()

    from lumen.blueprints.chat import routes as chat_routes
    monkeypatch.setattr(chat_routes, "send_message_stream", _fake_stream)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert '"done": true' in resp.get_data(as_text=True)
    assert _conversation_counter(app, test_user["id"]) == 1

    resp = auth_client.post("/chat/stream", json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "more"},
        ],
        "model": test_model["model_name"],
    })
    assert '"done": true' in resp.get_data(as_text=True)
    assert _conversation_counter(app, test_user["id"]) == 1

    with app.app_context():
        from sqlalchemy import func, select

        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        assert db.session.scalar(select(func.count(Conversation.id))) == 0


def test_chat_stream_group_grant_passes_access(app, auth_client, test_user, test_model):
    """A group grant on an owned model clears the access gate (stream starts, fails at LLM level)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from tests.conftest import grant_model_to_group, make_group_with_member, set_model_owner
        _grant_unlimited_pool(app, test_user["id"])
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)
        group_id = make_group_with_member(test_user["id"])
        grant_model_to_group(test_model["id"], group_id)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code != HTTPStatus.FORBIDDEN


def test_chat_stream_expired_model_rejected(app, auth_client, test_user, test_model):
    """A model past its end_date is treated like an unknown model on /chat/stream."""
    from datetime import timedelta
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.timeutils import utcnow
        db.session.get(ModelConfig, test_model["id"]).end_date = utcnow() - timedelta(days=1)
        db.session.commit()
    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_page_excludes_expired_model(app, auth_client, test_user, test_model, test_model_endpoint):
    from datetime import timedelta
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.timeutils import utcnow
        db.session.get(ModelConfig, test_model["id"]).end_date = utcnow() - timedelta(days=1)
        db.session.commit()
    resp = auth_client.get("/chat")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_chat_page_excludes_needs_ack_model_without_coin_pool(
    app, auth_client, test_model, test_model_endpoint,
):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.get("/chat")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_chat_page_shows_needs_ack_model_with_coin_pool(
    app, auth_client, test_user, test_model, test_model_endpoint,
):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.get("/chat")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_chat_stream_disconnect_is_not_reported_as_empty_response(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """A departed client is not an empty model response.

    send_message_stream stops without emitting its final result tuple when the
    disconnect flag is set, which leaves `result is None` — the same state as a
    genuinely empty response. Reporting the two identically is wrong in the log
    and, on a half-open connection where the socket is still live, delivers
    "Empty response from model" to a client that just received partial output.
    """
    import threading

    disconnected = threading.Event()
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        yield "Hello", None, None
        disconnected.set()  # client vanishes mid-stream
        yield " world", None, None

    from lumen.blueprints.chat import routes as chat_routes
    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)
    monkeypatch.setattr(chat_routes, "client_disconnect_event", lambda: disconnected)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })
    assert resp.status_code == HTTPStatus.OK
    body = b"".join(resp.response)
    resp.close()
    assert b"Empty response from model" not in body, (
        "a client disconnect was reported to the client as an empty model response"
    )


# ---------------------------------------------------------------------------
# Request timing columns
#
# The chat path is the only one where the LLM call lives in llm.py rather than
# in the view, so the marks have to travel view -> send_message_stream ->
# context-free generator. The real send_message_stream runs here (only the
# openai client is faked) so that journey is actually exercised.
# ---------------------------------------------------------------------------

_MAX_PLAUSIBLE_SPAN = 60 * 60  # seconds; a test request takes milliseconds
_QUEUE_WAIT = 0.05             # seconds of admission wait to stamp T0 behind
_SEND_BLOCKED = 0.25           # seconds the responder reports blocked in send


def _bridge_environ():
    """The environ keys ``asgi.py`` publishes; the test client bypasses it.

    ``lumen.queue_wait`` is left out on purpose — the real before_request hook
    derives it from the arrival mark.
    """
    import time
    from datetime import datetime, timezone

    from lumen.services.wsgi_disconnect import SendBlocked
    return {
        "lumen.t0_monotonic": time.monotonic() - _QUEUE_WAIT,
        "lumen.started_at": datetime.now(timezone.utc),
        "lumen.send_blocked": SendBlocked(),
    }


class _Chunk:
    """One upstream streaming chunk: a reasoning delta, a content delta, or usage."""

    def __init__(self, content=None, reasoning=None, usage=None):
        self.usage = usage
        delta = type("Delta", (), {
            "content": content, "reasoning_content": reasoning, "reasoning": None,
        })()
        self.choices = [type("Choice", (), {"delta": delta})()] if (content or reasoning) else []


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    completion_tokens_details = None


def _fake_openai(chunks):
    from unittest.mock import MagicMock
    client = MagicMock()
    client.chat.completions.create.return_value = iter(chunks)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=client)


def _allow_model(app, test_user, test_model):
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])


def _only_log(app):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        return db.session.execute(select(RequestLog)).scalar_one()


def test_chat_stream_records_timing_columns(
    app, auth_client, test_user, test_model, test_model_endpoint,
):
    """A reasoning delta ahead of the content splits ttft from ttft_visible.

    send_blocked is mutated after the first event is out: a float captured in
    the view would still read 0.0 there, since nothing had been sent yet.
    """
    from unittest.mock import patch
    _allow_model(app, test_user, test_model)
    chunks = [_Chunk(reasoning="thinking"), _Chunk(content="hi"), _Chunk(usage=_Usage())]

    environ = _bridge_environ()
    with patch("lumen.services.llm.openai.OpenAI", _fake_openai(chunks)):
        resp = auth_client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}],
                  "model": test_model["model_name"]},
            environ_base=environ,
        )
        assert resp.status_code == HTTPStatus.OK
        assert resp.is_streamed
        events = iter(resp.response)
        try:
            next(events)  # one event out; the generator is suspended mid-stream
            environ["lumen.send_blocked"].seconds = _SEND_BLOCKED
            for _ in events:
                pass
        finally:
            resp.close()

    log = _only_log(app)
    assert log.started_at is not None
    assert _QUEUE_WAIT <= log.queue_wait < _MAX_PLAUSIBLE_SPAN
    assert 0 <= log.preflight < _MAX_PLAUSIBLE_SPAN
    # The thinking phase sits between the two marks.
    assert 0 < log.ttft < log.ttft_visible < _MAX_PLAUSIBLE_SPAN
    assert log.send_blocked == _SEND_BLOCKED
    assert log.outcome == "ok"
    assert log.aborted is False


def test_chat_stream_without_the_bridge_records_nulls(
    app, auth_client, test_user, test_model, test_model_endpoint,
):
    """Absent marks are NULL — "not measured", not "measured as zero"."""
    from unittest.mock import patch
    _allow_model(app, test_user, test_model)
    chunks = [_Chunk(content="hi"), _Chunk(usage=_Usage())]

    with patch("lumen.services.llm.openai.OpenAI", _fake_openai(chunks)):
        resp = auth_client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}],
                  "model": test_model["model_name"]},
        )
        assert resp.status_code == HTTPStatus.OK
        b"".join(resp.response)
        resp.close()

    log = _only_log(app)
    assert log.started_at is None
    assert log.queue_wait is None
    assert log.preflight is None
    assert log.send_blocked is None
    assert log.ttft is not None
    assert log.ttft_visible is not None
    assert log.outcome == "ok"


class _SetOnCheck:
    """A disconnect flag that flips on the Nth ``is_set()`` call.

    The chunk loop in ``send_message_stream`` polls the flag before yielding and
    ``generate()`` polls it after receiving, so a real client can vanish in
    between. Counting the calls is the only way to land a disconnect in that
    one-statement window deterministically.
    """

    def __init__(self, flip_after):
        self._flip_after = flip_after
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls > self._flip_after


def test_chat_stream_saves_the_conversation_when_the_client_leaves_during_billing(
    app, auth_client, test_user, test_model, test_model_endpoint, monkeypatch,
):
    """A disconnect inside the billing window must not throw away the reply.

    ``send_message_stream`` bills the stream — coins subtracted, request_logs
    written with outcome "ok" and aborted false — between the last upstream
    chunk and the final result tuple it yields. A client that leaves in that
    window has already paid for the reply, so the conversation must still be
    written. Breaking out of the loop there instead loses the reply with nothing
    in request_logs to say it happened.
    """
    import threading
    from unittest.mock import patch

    _allow_model(app, test_user, test_model)
    disconnected = threading.Event()

    def chunks():
        yield _Chunk(content="hi")
        yield _Chunk(usage=_Usage())
        # Runs when the chunk loop asks for the next chunk: after the flag check
        # on the usage chunk above, and before the billing block. Exactly the
        # window this test is about.
        disconnected.set()

    from lumen.blueprints.chat import routes as chat_routes
    from lumen.services import llm as llm_service
    monkeypatch.setattr(chat_routes, "client_disconnect_event", lambda: disconnected)
    monkeypatch.setattr(llm_service, "client_disconnect_event", lambda: disconnected)

    with patch("lumen.services.llm.openai.OpenAI", _fake_openai(chunks())):
        resp = auth_client.post("/chat/stream", json={
            "messages": [{"role": "user", "content": "hi"}],
            "model": test_model["model_name"],
        })
        assert resp.status_code == HTTPStatus.OK
        b"".join(resp.response)
        resp.close()

    log = _only_log(app)
    assert log.outcome == "ok", "a stream that completed and billed was logged as something else"
    assert log.aborted is False

    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.message import Message
        stored = db.session.execute(select(Message).order_by(Message.id)).scalars().all()
    assert [m.role for m in stored] == ["user", "assistant"], (
        "the reply was billed but the conversation was not saved"
    )
    assert stored[1].content == "hi"


def test_chat_stream_bills_the_abort_without_waiting_for_the_stream_to_be_collected(
    app, auth_client, test_user, test_model, test_model_endpoint, monkeypatch,
):
    """``generate()`` closes the LLM stream itself rather than leaving it to GC.

    When ``generate()``'s own disconnect check wins the race, the
    ``send_message_stream`` generator is left suspended at a yield, and the
    ``except GeneratorExit`` handler that bills an abandoned stream only runs
    once that generator object is destroyed. Under refcounting that is usually
    immediate — but anything still referencing the frame (a traceback, a log
    record carrying exc_info, a profiler) postpones the abort indefinitely, and
    an exception raised during collection is swallowed. Here the reference is
    the test's own, standing in for all of them.
    """
    from unittest.mock import patch

    _allow_model(app, test_user, test_model)
    # Call 1 is the chunk loop's check on the content chunk (still connected);
    # call 2 is generate()'s check on the tuple it just received.
    disconnected = _SetOnCheck(flip_after=1)

    from lumen.blueprints.chat import routes as chat_routes
    from lumen.services import llm as llm_service
    monkeypatch.setattr(chat_routes, "client_disconnect_event", lambda: disconnected)
    monkeypatch.setattr(llm_service, "client_disconnect_event", lambda: disconnected)

    held = []
    real_send = chat_routes.send_message_stream

    def capturing(*args, **kwargs):
        stream = real_send(*args, **kwargs)
        held.append(stream)
        return stream

    monkeypatch.setattr(chat_routes, "send_message_stream", capturing)

    chunks = [_Chunk(content="hi"), _Chunk(content=" there"), _Chunk(usage=_Usage())]
    try:
        with patch("lumen.services.llm.openai.OpenAI", _fake_openai(chunks)):
            resp = auth_client.post("/chat/stream", json={
                "messages": [{"role": "user", "content": "hi"}],
                "model": test_model["model_name"],
            })
            assert resp.status_code == HTTPStatus.OK
            b"".join(resp.response)
            resp.close()

        assert disconnected.calls >= 2, "the disconnect never landed in the intended window"
        with app.app_context():
            from sqlalchemy import select

            from lumen.extensions import db
            from lumen.models.request_log import RequestLog
            logs = db.session.execute(select(RequestLog)).scalars().all()
        assert len(logs) == 1, (
            "the abandoned stream was not billed; the abort waited on the "
            "generator being collected"
        )
        assert logs[0].aborted is True
        assert logs[0].outcome == "disconnect"
    finally:
        for stream in held:
            stream.close()


# ---------------------------------------------------------------------------
# Live state (in-flight accounting)
#
# admit() belongs in the view, after every rejection path; release() in the
# generator's finally, which runs context-free — no db.session, no current_app.
# Every exit path must leave the in-flight count at zero.
# ---------------------------------------------------------------------------

class _RecordingLiveState:
    """Wraps a real LocalLiveState and counts the two call sites.

    Wrapping rather than subclassing keeps this tied to the seam's public
    interface (admit/release/snapshot) and nothing else.
    """

    def __init__(self, inner):
        self._inner = inner
        self.admits = 0
        self.releases = 0

    def admit(self, model_key, entity_id):
        self.admits += 1
        return self._inner.admit(model_key, entity_id)

    def release(self, ticket):
        self.releases += 1
        self._inner.release(ticket)

    @property
    def inflight(self):
        return sum(m.inflight for m in self._inner.snapshot().values())


def _recording_live_state(monkeypatch, *modules):
    """Give the named route modules a fresh, counted live state for one test."""
    from lumen.services.live_state import LocalLiveState
    state = _RecordingLiveState(LocalLiveState())
    for module in modules:
        monkeypatch.setattr(module, "get_live_state", lambda state=state: state)
    return state


_FINAL_RESULT = {
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


def _post_stream(auth_client, test_model):
    return auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": test_model["model_name"],
    })


def test_chat_stream_admits_once_and_releases_once(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """One admit in the view, one release in the generator, zero left over —
    and the request counts as live until it completes, not until the first
    token."""
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        yield "Hello", None, None
        yield None, None, dict(_FINAL_RESULT)

    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = _post_stream(auth_client, test_model)
    assert resp.status_code == HTTPStatus.OK
    assert resp.is_streamed
    events = iter(resp.response)
    try:
        next(events)  # suspended mid-stream: still in flight
        assert state.admits == 1
        assert state.inflight == 1
        for _ in events:
            pass
    finally:
        resp.close()

    assert state.releases == 1
    assert state.inflight == 0


def test_chat_stream_releases_when_the_generator_raises(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """A KeyError inside the billing block still leaves the count at zero."""
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        yield "Hello", None, None
        yield None, None, {"model": "test-model"}  # no "reply" → KeyError

    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = _post_stream(auth_client, test_model)
    body = b"".join(resp.response)
    resp.close()
    assert b'"error"' in body
    assert state.admits == 1
    assert state.releases == 1
    assert state.inflight == 0


def test_chat_stream_releases_when_upstream_fails(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """An upstream failure raised out of send_message_stream releases too."""
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        raise RuntimeError("upstream is down")
        yield  # pragma: no cover - makes this a generator function

    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = _post_stream(auth_client, test_model)
    b"".join(resp.response)
    resp.close()
    assert state.admits == 1
    assert state.inflight == 0


def test_chat_stream_releases_on_client_disconnect(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """The client vanishes mid-stream: the generator returns early and the
    ticket goes with it."""
    import threading

    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)
    disconnected = threading.Event()

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        yield "Hello", None, None
        disconnected.set()
        yield " world", None, None

    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)
    monkeypatch.setattr(chat_routes, "client_disconnect_event", lambda: disconnected)

    resp = _post_stream(auth_client, test_model)
    b"".join(resp.response)
    resp.close()
    assert state.admits == 1
    assert state.releases == 1
    assert state.inflight == 0


def test_chat_stream_releases_when_the_body_is_abandoned(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """Closing the response mid-stream (GeneratorExit) releases the ticket."""
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        yield "Hello", None, None
        yield " world", None, None
        yield None, None, dict(_FINAL_RESULT)

    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = _post_stream(auth_client, test_model)
    events = iter(resp.response)
    next(events)
    assert state.inflight == 1
    resp.close()  # client goes away with events still pending
    assert state.releases == 1
    assert state.inflight == 0


def test_rejected_chat_stream_is_never_admitted(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """A blocked model is refused before admit, so it never counts as live."""
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from tests.conftest import set_model_owner
        _grant_unlimited_pool(app, test_user["id"])
        owner = Entity(entity_type="user", email="rejected-owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)
    state = _recording_live_state(monkeypatch, chat_routes)

    resp = _post_stream(auth_client, test_model)
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert state.admits == 0
    assert state.inflight == 0


def test_unknown_model_is_never_admitted(app, auth_client, test_user, monkeypatch):
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)

    resp = auth_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": "no-such-model",
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert state.admits == 0
    assert state.inflight == 0


def test_releasing_a_finished_stream_twice_leaves_the_count_intact(
    app, auth_client, test_user, test_model, monkeypatch,
):
    """close() after the generator has already run its finally must not
    decrement anything else."""
    from lumen.blueprints.chat import routes as chat_routes
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
    state = _recording_live_state(monkeypatch, chat_routes)
    bystander = state.admit(test_model["model_name"], 4242)

    def fake_stream(messages, model, entity_id=None, source="chat", effective=None):
        yield "Hello", None, None
        yield None, None, dict(_FINAL_RESULT)

    monkeypatch.setattr(chat_routes, "send_message_stream", fake_stream)

    resp = _post_stream(auth_client, test_model)
    b"".join(resp.response)
    resp.close()          # first release ran at StopIteration
    resp.close()          # and again — harmless
    assert state.inflight == 1, "the double release took another request's ticket"

    state.release(bystander)
    state.release(bystander)
    assert state.inflight == 0
