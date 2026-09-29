"""The Anthropic Messages surface: /v1/messages and /v1/messages/count_tokens.

Protocol translation is covered in tests/unit/test_anthropic_protocol.py; this
file covers the route — authentication, the Lumen policy gates, billing, error
envelopes, and the official SDK talking to a running server.
"""
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import pytest
from flask import has_app_context, has_request_context
from openai import OpenAI
from openai.types.chat import ChatCompletionChunk
from werkzeug.serving import make_server

from lumen.extensions import db
from lumen.models.model_endpoint import ModelEndpoint
from scripts.anthropic_toolbot import app as toolbot
from tests.routes.test_api_auth import (
    _allow_model,
    _capturing_openai,
    _fake_openai,
    _grant_unlimited_pool,
    _make_openai_error,
    api_key,
    fresh_rate_limit,
    inactive_api_key,
)
from tests.unit.test_anthropic_protocol import _chunk, _tool, claude_code_request

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "claude_code_request", "fresh_rate_limit", "inactive_api_key"]


HEADERS = {"anthropic-version": "2023-06-01", "content-type": "application/json"}


def _body(model="test-model", **overrides):
    return {"model": model, "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}], **overrides}


def _post(client, token, body=None, path="/v1/messages", data=None, **extra_headers):
    """POST to the Anthropic surface. ``data`` sends a raw (possibly invalid) body."""
    return client.post(path, headers={"x-api-key": token, **HEADERS, **extra_headers},
                       data=data if data is not None else json.dumps(_body() if body is None else body))


def _sse(resp):
    """Parse an SSE body into (event name, data dict) pairs."""
    events = []
    for block in resp.get_data(as_text=True).split("\n\n"):
        if block.strip():
            name, data = block.strip().split("\n")[:2]
            events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def _request_logs(app):
    """Every RequestLog row, as plain dicts (the session closes with the context)."""
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        return [
            {"source": log.source, "input_tokens": log.input_tokens,
             "output_tokens": log.output_tokens, "aborted": log.aborted}
            for log in db.session.execute(select(RequestLog)).scalars().all()
        ]


def _give_model_to_someone_else(app, test_model):
    """Make the model owned by another entity, so the test user has no access."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from tests.conftest import set_model_owner
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)


class _ToolCallResponse:
    """A non-streaming upstream reply that ended in a tool call."""

    class usage:  # noqa: N801 - stands in for the OpenAI usage object
        prompt_tokens = 11
        completion_tokens = 5

    def model_dump(self):
        return {"id": "chatcmpl-1", "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}},
            ]},
            "finish_reason": "tool_calls",
        }], "usage": {"prompt_tokens": 11, "completion_tokens": 5}}


class _TextResponse:
    class usage:  # noqa: N801
        prompt_tokens = 7
        completion_tokens = 3

    def model_dump(self):
        return {"id": "chatcmpl-1", "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


class _Chunk:
    """An upstream streaming chunk, as the OpenAI SDK hands it over."""

    def __init__(self, payload, usage=None):
        self._payload = payload
        self.usage = usage
        self.choices = payload.get("choices") or []

    def model_dump(self):
        return self._payload


class _Usage:
    prompt_tokens = 5
    completion_tokens = 7


def _delta(content, finish=None):
    return _Chunk({"id": "chatcmpl-1", "choices": [
        {"index": 0, "delta": {"content": content}, "finish_reason": finish}]})


def _text_stream():
    yield _delta("hel")
    yield _delta("lo", finish="stop")
    yield _Chunk({"id": "chatcmpl-1", "choices": [],
                  "usage": {"prompt_tokens": 5, "completion_tokens": 7}}, usage=_Usage())


@pytest.fixture
def api(app, client, test_user, test_model, test_model_endpoint, api_key, monkeypatch,
        fresh_rate_limit):
    """An authenticated caller with access to the test model and a stub upstream.

    Most tests here are about the route rather than its setup, and the setup
    needs eight fixtures; this collapses them into one. Tests that need the
    opposite of some part of it (no endpoint, no access, an exhausted budget)
    take the underlying fixtures directly instead.
    """
    from lumen.blueprints.api import routes
    _allow_model(app, test_user, test_model)
    token, key_id = api_key
    _capturing_openai(monkeypatch, routes, lambda **kw: _TextResponse())

    return SimpleNamespace(
        token=token, key_id=key_id, model=test_model["model_name"],
        post=lambda body=None, **kw: _post(client, token, body, **kw),
        # Replace the stub: `upstream(fn)` for a non-streaming reply, `stream(chunks)`
        # for a streaming one.
        upstream=lambda create: _capturing_openai(monkeypatch, routes, create),
        stream=lambda chunks: _fake_openai(monkeypatch, routes, chunks),
        routes=routes, monkeypatch=monkeypatch,
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    pytest.param(lambda t: {"x-api-key": t}, id="x-api-key"),
    pytest.param(lambda t: {"Authorization": f"Bearer {t}"}, id="bearer"),
    pytest.param(lambda t: {"x-api-key": t, "Authorization": f"Bearer {t}"}, id="both-agreeing"),
])
def test_accepted_key_headers(client, api, headers):
    resp = client.post("/v1/messages", headers={**HEADERS, **headers(api.token)},
                       data=json.dumps(_body()))
    assert resp.status_code == HTTPStatus.OK


def test_conflicting_auth_headers_are_refused(client, api_key, fresh_rate_limit):
    token, _ = api_key
    resp = _post(client, token, Authorization="Bearer something-else")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.get_json()["error"]["type"] == "authentication_error"


def test_lowercase_bearer_is_checked_against_x_api_key(client, api_key, fresh_rate_limit):
    """The auth scheme is case-insensitive, so this is still a second key."""
    token, _ = api_key
    resp = _post(client, token, Authorization="bearer something-else")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


def test_lowercase_bearer_alone_authenticates(client, api):
    resp = client.post("/v1/messages", headers={"Authorization": f"bearer {api.token}", **HEADERS},
                       data=json.dumps(_body(model=api.model)))
    assert resp.status_code == HTTPStatus.OK


def test_missing_key(client, fresh_rate_limit):
    resp = client.post("/v1/messages", headers=HEADERS, data=json.dumps(_body()))
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    body = resp.get_json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


def test_unknown_key(client, fresh_rate_limit):
    assert _post(client, "not-a-key").status_code == HTTPStatus.UNAUTHORIZED


def test_inactive_key(client, inactive_api_key, fresh_rate_limit):
    assert _post(client, inactive_api_key).status_code == HTTPStatus.UNAUTHORIZED


def test_non_ascii_api_key_is_refused_not_crashed(client, api_key, fresh_rate_limit):
    """Both headers are raw client input; comparing them as non-ASCII strings
    raised, turning an unauthenticated request into a 500."""
    resp = client.post("/v1/messages",
                       headers={"x-api-key": "ÿabc", "Authorization": "Bearer xyz", **HEADERS},
                       data=json.dumps(_body()))
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.get_json()["error"]["type"] == "authentication_error"


def test_disabled_entity(app, client, test_user, api_key, fresh_rate_limit):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        db.session.get(Entity, test_user["id"]).active = False
        db.session.commit()

    resp = _post(client, token)
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["type"] == "permission_error"


def test_monitor_token_cannot_send_messages(app, client, fresh_rate_limit):
    monitor = "monitor-secret-anthropic"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "api": {"monitoring": {"token": monitor}}}
    try:
        resp = _post(client, monitor)
        assert resp.status_code == HTTPStatus.FORBIDDEN
        assert resp.get_json()["error"]["type"] == "permission_error"
    finally:
        app.config["YAML_DATA"].pop("api", None)


# ---------------------------------------------------------------------------
# Headers and body validation
#
# What each field means is settled in the unit tests; these prove the wiring —
# that a refusal from the translation reaches the client as a 400 in the
# Anthropic envelope, and that the raw-body cases never reach it at all.
# ---------------------------------------------------------------------------

def test_unsupported_version_is_refused(client, api_key, fresh_rate_limit):
    token, _ = api_key
    resp = _post(client, token, **{"anthropic-version": "2099-01-01"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["type"] == "invalid_request_error"


def test_missing_version_is_allowed_and_unknown_beta_is_refused(client, api):
    assert api.post().status_code == HTTPStatus.OK
    resp = api.post(**{"anthropic-beta": "made-up-2099-01-01"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["type"] == "invalid_request_error"


def test_unsupported_feature_fails_explicitly(client, api_key, fresh_rate_limit):
    token, _ = api_key
    resp = _post(client, token, _body(mcp_servers=[{"type": "url", "url": "https://x", "name": "n"}]))
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "mcp_servers" in resp.get_json()["error"]["message"]


def test_wrong_content_type(client, api_key, fresh_rate_limit):
    token, _ = api_key
    resp = client.post("/v1/messages", headers={"x-api-key": token, "content-type": "text/plain"},
                       data="model=test-model")
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("raw", [
    pytest.param("{nope", id="malformed"),
    pytest.param("", id="empty"),
    pytest.param("[1, 2]", id="not-an-object"),
    pytest.param("[" * 4000 + "]" * 4000, id="nested-too-deeply"),
])
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens"])
def test_unusable_body(client, api_key, fresh_rate_limit, raw, path):
    """Deeply nested JSON exhausts the stack inside the parser; unguarded it
    escaped Flask as an HTML page no Anthropic client can read."""
    token, _ = api_key
    resp = _post(client, token, path=path, data=raw)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["type"] == "error"


def test_wrong_method_is_an_anthropic_error(client, fresh_rate_limit):
    resp = client.get("/v1/messages")
    assert resp.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert resp.get_json()["type"] == "error"


def test_unknown_path_under_messages_is_an_anthropic_error(client, fresh_rate_limit):
    resp = client.post("/v1/messages/typo", headers=HEADERS, data="{}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert resp.get_json()["error"]["type"] == "not_found_error"


# ---------------------------------------------------------------------------
# Lumen policy gates, in the Anthropic envelope
# ---------------------------------------------------------------------------

def test_unknown_model_is_not_found(client, api_key, fresh_rate_limit):
    token, _ = api_key
    resp = _post(client, token, _body(model="nope"))
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert resp.get_json()["error"]["type"] == "not_found_error"


def test_no_access_is_a_permission_error(app, client, test_model, test_model_endpoint,
                                         api_key, fresh_rate_limit):
    token, _ = api_key
    _give_model_to_someone_else(app, test_model)
    resp = _post(client, token)
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["type"] == "permission_error"


def test_consent_required_is_a_permission_error(app, client, test_user, test_model,
                                                api_key, fresh_rate_limit):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        _grant_unlimited_pool(app, test_user["id"])
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = _post(client, token)
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["type"] == "permission_error"


def test_exhausted_budget_is_a_rate_limit_error(app, client, test_user, test_model,
                                                test_model_endpoint, api_key, fresh_rate_limit):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=10,
                                   refresh_coins=1, starting_coins=10))
        db.session.add(EntityBalance(entity_id=test_user["id"], coins_left=0))
        db.session.commit()

    resp = _post(client, token)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert resp.get_json()["error"]["type"] == "rate_limit_error"


def test_no_healthy_endpoint_is_overloaded(app, client, test_user, test_model,
                                           api_key, fresh_rate_limit):
    token, _ = api_key
    _allow_model(app, test_user, test_model)  # no endpoint
    resp = _post(client, token)
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.get_json()["error"]["type"] == "overloaded_error"


def test_streaming_preflight_rejection_is_a_plain_error(app, client, test_user, test_model,
                                                        api_key, fresh_rate_limit):
    """Nothing has been streamed yet, so it is an HTTP error, not an SSE event."""
    token, _ = api_key
    _allow_model(app, test_user, test_model)  # no endpoint -> 503
    resp = _post(client, token, _body(stream=True))
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.get_json()["error"]["type"] == "overloaded_error"


def test_rate_limit_uses_the_anthropic_envelope(app, client, api):
    """The limiter answers before the view runs, so the envelope comes from the
    app-level handler, which must know this path speaks Anthropic."""
    saved = app.config.get("YAML_DATA", {})
    app.config["YAML_DATA"] = {**saved, "rate_limiting": {"limit": "1 per minute"}}
    try:
        api.post()
        resp = api.post()
        assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
        body = resp.get_json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "rate_limit_error"
        assert resp.headers.get("Retry-After")
    finally:
        app.config["YAML_DATA"] = saved


def test_upstream_failure_is_an_api_error(api):
    def boom(**kwargs):
        raise RuntimeError("upstream is down")

    api.upstream(boom)
    resp = api.post()
    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    body = resp.get_json()
    assert body["error"]["type"] == "api_error"
    assert "upstream is down" not in json.dumps(body)


def test_upstream_4xx_passes_through_as_an_invalid_request(api):
    import openai

    exc = _make_openai_error(openai.BadRequestError, 400, {
        "error": {"message": "This model's maximum context length is 4096 tokens",
                  "type": "invalid_request_error"}})

    def boom(**kwargs):
        raise exc

    api.upstream(boom)
    resp = api.post()
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    body = resp.get_json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "maximum context length" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Non-streaming responses and billing
# ---------------------------------------------------------------------------

def test_non_streaming_shape_and_billing(app, api):
    resp = api.post(_body(system="be brief"))
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == api.model
    assert body["content"] == [{"type": "text", "text": "hello"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 3}

    assert _request_logs(app) == [
        {"source": "api", "input_tokens": 7, "output_tokens": 3, "aborted": False}]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        assert db.session.get(APIKey, api.key_id).requests == 1


def test_non_streaming_tool_use(api):
    api.upstream(lambda **kw: _ToolCallResponse())
    body = api.post(_body(tools=[
        {"name": "bash", "description": "run", "input_schema": {"type": "object"}}])).get_json()
    assert body["stop_reason"] == "tool_use"
    assert body["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"cmd": "ls"}}]


def test_the_request_reaching_upstream_is_translated(api):
    """System prompt, sampling params and the per-entity cache salt all arrive."""
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return _TextResponse()

    api.upstream(create)
    api.post(_body(system="be brief", temperature=0.2, top_k=40, stop_sequences=["END"]))
    assert seen["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["max_tokens"] == 64
    assert seen["temperature"] == 0.2
    assert seen["stop"] == ["END"]
    assert seen["extra_body"]["top_k"] == 40
    assert seen["extra_body"]["cache_salt"]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_streaming_event_order_and_billing(app, api):
    api.stream(_text_stream())
    resp = api.post(_body(stream=True))
    assert resp.status_code == HTTPStatus.OK
    assert resp.content_type.startswith("text/event-stream")
    events = _sse(resp)

    assert [name for name, _ in events] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert events[0][1]["message"]["model"] == api.model
    assert "".join(e[1]["delta"]["text"] for e in events[2:4]) == "hello"
    assert events[5][1]["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert b"data: [DONE]" not in resp.get_data()

    assert _request_logs(app) == [
        {"source": "api", "input_tokens": 5, "output_tokens": 7, "aborted": False}]


def test_streaming_upstream_failure_emits_an_error_event(api):
    def boom(**kwargs):
        raise RuntimeError("upstream is down")

    api.upstream(boom)
    events = _sse(api.post(_body(stream=True)))
    assert events[-1][0] == "error"
    assert events[-1][1]["type"] == "error"
    assert "upstream is down" not in json.dumps(events[-1][1])


def test_streaming_chunk_with_a_null_error_field_is_not_an_error(api):
    """Some backends send "error": null on ordinary chunks."""
    api.stream([
        _Chunk({"id": "chatcmpl-1", "error": None,
                "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]}),
        _Chunk({"id": "chatcmpl-1", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
               usage=_Usage()),
    ])
    names = [name for name, _ in _sse(api.post(_body(stream=True)))]
    assert "error" not in names
    assert names[-1] == "message_stop"


def test_streaming_disconnect_bills_an_abort(app, api):
    import threading
    disconnected = threading.Event()

    def chunks():
        yield _delta("hel")
        disconnected.set()  # client vanishes mid-stream
        yield _delta("lo")

    api.stream(chunks())
    api.monkeypatch.setattr(api.routes, "client_disconnect_event", lambda: disconnected)

    resp = api.post(_body(stream=True))
    b"".join(resp.response)
    resp.close()

    assert [log["aborted"] for log in _request_logs(app)] == [True]


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------

def _count(api, **body):
    return api.post({"model": api.model, "messages": [{"role": "user", "content": "hi"}], **body},
                    path="/v1/messages/count_tokens")


def test_count_tokens(api):
    resp = _count(api, messages=[{"role": "user", "content": "x" * 40}])
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == {"input_tokens": 10}


def test_count_tokens_counts_the_tool_catalogue(api):
    plain = _count(api).get_json()["input_tokens"]
    with_tools = _count(api, tools=[
        {"name": "Bash", "description": "d" * 400,
         "input_schema": {"type": "object"}}]).get_json()["input_tokens"]
    assert with_tools > plain + 50


def test_count_tokens_is_not_billed(app, api):
    _count(api)
    assert _request_logs(app) == []


def test_count_tokens_checks_model_access(app, client, test_model, api_key, fresh_rate_limit):
    token, _ = api_key
    _give_model_to_someone_else(app, test_model)
    resp = _post(client, token, {"model": test_model["model_name"],
                                 "messages": [{"role": "user", "content": "hi"}]},
                 path="/v1/messages/count_tokens")
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Contract: a real Claude Code request
# ---------------------------------------------------------------------------

def test_a_real_claude_code_request_is_served(client, api, claude_code_request):
    """The captured claude-cli 2.1.280 request, replayed against the route."""
    api.stream(_text_stream())
    headers = {k: v for k, v in claude_code_request["headers"].items() if k.lower() != "x-api-key"}
    resp = client.post("/v1/messages", headers={"x-api-key": api.token, **headers},
                       data=json.dumps({**claude_code_request["body"], "model": api.model}))
    assert resp.status_code == HTTPStatus.OK
    assert [name for name, _ in _sse(resp)][0] == "message_start"


# ---------------------------------------------------------------------------
# The official SDK against a running server
# ---------------------------------------------------------------------------

@pytest.fixture
def live_lumen(app, api):
    """Serve the app on a real socket so the official SDK can talk to it."""
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_official_sdk_non_streaming(live_lumen, api):
    anthropic = pytest.importorskip("anthropic")
    message = anthropic.Anthropic(base_url=live_lumen, api_key=api.token).messages.create(
        model=api.model, max_tokens=64, messages=[{"role": "user", "content": "hi"}])
    assert message.content[0].text == "hello"
    assert message.stop_reason == "end_turn"
    assert (message.usage.input_tokens, message.usage.output_tokens) == (7, 3)


def test_official_sdk_streaming(live_lumen, api):
    anthropic = pytest.importorskip("anthropic")
    api.stream(_text_stream())
    client = anthropic.Anthropic(base_url=live_lumen, api_key=api.token)
    with client.messages.stream(model=api.model, max_tokens=64,
                                messages=[{"role": "user", "content": "hi"}]) as stream:
        text = stream.get_final_text()
        final = stream.get_final_message()
    assert text == "hello"
    assert final.stop_reason == "end_turn"
    assert final.usage.output_tokens == 7


def test_official_sdk_surfaces_errors(live_lumen, api):
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.Anthropic(base_url=live_lumen, api_key=api.token, max_retries=0)
    with pytest.raises(anthropic.NotFoundError):
        client.messages.create(model="nope", max_tokens=8,
                               messages=[{"role": "user", "content": "hi"}])


@pytest.mark.parametrize('overrides', [
    {'system': [{'type': 'text', 'text': 42}]},
    {'messages': [{'role': 'assistant', 'content': [{'type': 'text', 'text': {}}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': False}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't',
                                             'content': [{'type': 'text', 'text': 42}]}]}]},
    {'tools': 42},
    {'temperature': float('nan')},
    {'top_p': True},
    {'top_k': -1},
    {'stop_sequences': 'END'},
    {'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': 'false'}},
    {'output_config': False},
    {'tool_choice': {'type': []}},
    {'messages': [{'role': 'user', 'content': [{'type': []}]}]},
    {'output_config': {'format': {'type': 'json_schema', 'schema': {'type': 'object'}}}},
    {'thinking': {'type': 'adaptive'}},
    {'thinking': {'type': 'enabled', 'budget_tokens': 1024}},
    {'context_management': {'edits': [{'type': 'clear_tool_uses_20250919'}]}},
    {'service_tier': 'priority'},
])
def test_invalid_or_unsupported_request_is_400(api, overrides):
    response = api.post(_body(**overrides))
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()['error']['type'] == 'invalid_request_error'


def test_non_ascii_key_with_monitor_enabled(app, client, api, monkeypatch):
    monkeypatch.setitem(app.config, 'YAML_DATA', {
        **app.config['YAML_DATA'], 'api': {'monitoring': {'token': 'monitor-secret'}}})
    response = client.post('/v1/messages', headers={'x-api-key': 'ÿabc'}, json=_body())
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_official_sdk_reassembles_interleaved_tools(live_lumen, api):
    chunks = [
        _tool(id='a', name='one', arguments='{"x":'),
        _tool(index=1, id='b', name='two', arguments='{"y":'),
        _tool(arguments='1}'),
        _chunk(content='two calls'),
        _tool(index=1, arguments='2}'),
        _chunk(finish='tool_calls'),
    ]
    # Use genuine upstream SDK chunks so routing and SDK parsing are exercised.
    api.stream([ChatCompletionChunk.model_validate({
        'object': 'chat.completion.chunk', 'created': 0, 'model': api.model, **chunk,
    }) for chunk in chunks])
    with anthropic.Anthropic(base_url=live_lumen, api_key=api.token) as client:
        with client.messages.stream(model=api.model, max_tokens=64,
                                    messages=[{'role': 'user', 'content': 'run both'}]) as stream:
            message = stream.get_final_message()
    calls = [b for b in message.content if b.type == 'tool_use']
    assert [(c.id, c.name, c.input) for c in calls] == [('a', 'one', {'x': 1}), ('b', 'two', {'y': 2})]
    assert message.stop_reason == 'tool_use'


@pytest.mark.parametrize("exit_path", ["normal", "disconnect", "billing_error"])
def test_anthropic_stream_releases_state_and_context(app, api, monkeypatch, exit_path):
    state = Mock()
    monkeypatch.setattr(api.routes, "get_live_state", lambda: state)
    api.stream(_text_stream())
    if exit_path == "billing_error":
        monkeypatch.setattr(api.routes, "_record_api_key_usage", Mock(side_effect=RuntimeError("private detail")))
    with app.app_context():
        pool = db.engine.pool
    response = api.post(_body(stream=True))
    def consume():
        chunks = []
        try:
            for chunk in response.response:
                assert not has_app_context() and not has_request_context()
                assert pool.checkedout() == 0
                chunks.append(chunk)
                if exit_path == "disconnect":
                    break
        finally:
            response.close()
        return chunks

    # pytest-flask holds a context on the test thread; WSGI workers do not.
    with ThreadPoolExecutor(max_workers=1) as worker:
        chunks = worker.submit(consume).result(timeout=5)
    state.admit.assert_called_once()
    state.release.assert_called_once_with(state.admit.return_value)
    logs = _request_logs(app)
    if exit_path == "billing_error":
        assert b'event: error' in b''.join(chunks)
        assert b'private detail' not in b''.join(chunks)
        assert logs == []
    else:
        assert len(logs) == 1 and logs[0]["aborted"] == (exit_path == "disconnect")


@pytest.mark.skipif(not shutil.which("claude"), reason="Claude Code is not installed")
@pytest.mark.parametrize("use_tool", [False, True])
@pytest.mark.timeout(60)
def test_claude_code_over_real_http(app, live_lumen, api, test_model_endpoint, monkeypatch, tmp_path, use_tool):
    """CLI -> Lumen -> OpenAI SDK -> local tool backend; no external inference."""
    backend = make_server("127.0.0.1", 0, toolbot, threaded=True)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(api.routes.openai, "OpenAI", OpenAI)
    with app.app_context():
        endpoint = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        endpoint.url = f"http://127.0.0.1:{backend.server_port}/v1"
        db.session.commit()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
    env.update(
        ANTHROPIC_BASE_URL=live_lumen, ANTHROPIC_API_KEY=api.token,
        ANTHROPIC_MODEL=api.model, ANTHROPIC_SMALL_FAST_MODEL=api.model,
        ANTHROPIC_DEFAULT_HAIKU_MODEL=api.model, CLAUDE_CONFIG_DIR=str(tmp_path),
        CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1", CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING="1",
        MAX_THINKING_TOKENS="0", CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "run echo hello-from-lumen" if use_tool else "say hello-from-lumen",
             "--model", api.model, "--tools", "Bash" if use_tool else "",
             "--allowedTools", "Bash", "--max-turns", "3"],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=45,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 0, result.stderr
        assert "hello-from-lumen" in result.stdout
        logs = _request_logs(app)
        assert len(logs) >= (2 if use_tool else 1)
        assert all(log["source"] == "api" and not log["aborted"] for log in logs)
    finally:
        backend.shutdown()
        thread.join(timeout=5)
