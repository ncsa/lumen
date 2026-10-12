"""CORS on the /v1 API: browser pages on any origin can call every endpoint.

Every /v1 response carries the CORS headers, errors and streams included, and a
preflight OPTIONS is answered before auth, the DB and the per-key rate limit.
"""

from datetime import timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import event

from lumen.extensions import db
from tests.routes.test_api_auth import fresh_rate_limit

# Re-exported so pytest resolves the fixture by name in this module's tests.
__all__ = ["fresh_rate_limit"]

ORIGIN = "https://app.example.org"
PREFLIGHT_HEADERS = {
    "Origin": ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization, content-type",
}
# Every header the official OpenAI JS SDK can send; a browser lists each one in
# Access-Control-Request-Headers, so a missing one fails the preflight.
OPENAI_JS_SDK_HEADERS = {
    "authorization", "content-type", "openai-organization", "openai-project", "openai-beta",
    "x-stainless-lang", "x-stainless-package-version", "x-stainless-os", "x-stainless-arch",
    "x-stainless-runtime", "x-stainless-runtime-version", "x-stainless-retry-count",
    "x-stainless-timeout", "x-stainless-helper-method",
}
V1_PATHS = ["/v1/models", "/v1/usage", "/v1/chat/completions", "/v1/audio/transcriptions"]


@pytest.fixture
def api_key(app, test_user):
    """Create an active API key for test_user. Returns the token."""
    token = "lk_test_cors_token"
    with app.app_context():
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        db.session.add(APIKey(entity_id=test_user["id"], name="cors-key", key_hash=hash_api_key(token)))
        db.session.commit()
    return token


def _grant_unlimited_pool(entity_id):
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
    db.session.commit()


def _exhaust_budget(entity_id):
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.timeutils import utcnow
    db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=10, starting_coins=100))
    db.session.add(EntityBalance(entity_id=entity_id, coins_left=0, last_refill_at=utcnow() - timedelta(minutes=30)))
    db.session.commit()


def _fake_openai(monkeypatch, create):
    from lumen.blueprints.api import routes

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chat(self):
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())


def _chat(client, token, stream=False):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": stream},
    )


def _assert_cors(resp):
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Retry-After" in resp.headers["Access-Control-Expose-Headers"]
    assert "Access-Control-Allow-Credentials" not in resp.headers


@pytest.mark.parametrize("path", V1_PATHS)
def test_preflight_answers_without_auth_or_db(app, client, path):
    statements = []

    def _count(*a, **k):
        statements.append(a)

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        resp = client.options(path, headers=PREFLIGHT_HEADERS)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert statements == []
    _assert_cors(resp)
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    allowed = {h.strip().lower() for h in resp.headers["Access-Control-Allow-Headers"].split(",")}
    assert OPENAI_JS_SDK_HEADERS <= allowed
    assert resp.headers["Access-Control-Max-Age"] == "600"


def test_preflights_do_not_use_up_the_per_key_rate_limit(
    app, client, monkeypatch, test_user, api_key, fresh_rate_limit,
):
    monkeypatch.setitem(app.config["YAML_DATA"]["rate_limiting"], "limit", "1 per minute")

    for _ in range(5):
        assert client.options("/v1/chat/completions", headers=PREFLIGHT_HEADERS).status_code == HTTPStatus.NO_CONTENT

    # Missing model/messages: a 400 from the view proves the limiter let it through.
    headers = {"Authorization": f"Bearer {api_key}", "Origin": ORIGIN}
    assert client.post("/v1/chat/completions", headers=headers, json={"x": 1}).status_code == HTTPStatus.BAD_REQUEST
    # The limit is live: the next real request is the one that trips it, with CORS on the 429.
    limited = client.post("/v1/chat/completions", headers=headers, json={"x": 1})
    assert limited.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert "Retry-After" in limited.headers
    _assert_cors(limited)


def test_successful_get_has_cors(client, api_key):
    resp = client.get("/v1/usage", headers={"Authorization": f"Bearer {api_key}", "Origin": ORIGIN})
    assert resp.status_code == HTTPStatus.OK
    _assert_cors(resp)


def test_unauthorized_has_cors(client):
    resp = client.get("/v1/models", headers={"Authorization": "Bearer nope", "Origin": ORIGIN})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    _assert_cors(resp)


def test_nonstreaming_chat_has_cors(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    with app.app_context():
        _grant_unlimited_pool(test_user["id"])
    _fake_openai(monkeypatch, lambda **kw: ChatCompletion(
        id="cmpl-x", object="chat.completion", created=1, model="dummy",
        choices=[Choice(index=0, message=ChatCompletionMessage(role="assistant", content="hi"), finish_reason="stop")],
        usage=CompletionUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
    ))

    resp = _chat(client, api_key)
    assert resp.status_code == HTTPStatus.OK
    _assert_cors(resp)


def test_streaming_chat_has_cors(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    class _Usage:
        prompt_tokens = 5
        completion_tokens = 7

    class _Chunk:
        def __init__(self, usage):
            self.usage = usage
            self.choices = []
            self.model = None

        def model_dump(self):
            return {"model": self.model, "choices": [{"delta": {"content": "hi"}}]}

    with app.app_context():
        _grant_unlimited_pool(test_user["id"])
    _fake_openai(monkeypatch, lambda **kw: iter([_Chunk(None), _Chunk(_Usage())]))

    resp = _chat(client, api_key, stream=True)
    assert resp.status_code == HTTPStatus.OK
    assert resp.mimetype == "text/event-stream"
    _assert_cors(resp)
    assert "data: [DONE]" in resp.get_data(as_text=True)


def test_insufficient_quota_has_cors_and_exposes_retry_after(
    app, client, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    with app.app_context():
        _exhaust_budget(test_user["id"])

    resp = _chat(client, api_key)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert resp.get_json()["error"]["code"] == "insufficient_quota"
    assert "Retry-After" in resp.headers
    _assert_cors(resp)


@pytest.mark.parametrize("method, path, status", [
    ("POST", "/v1/nope", HTTPStatus.NOT_FOUND),
    ("OPTIONS", "/v1/nope", HTTPStatus.NOT_FOUND),
    ("GET", "/v1/chat/completions", HTTPStatus.METHOD_NOT_ALLOWED),
])
def test_routing_errors_under_v1_have_cors(client, method, path, status):
    resp = client.open(path, method=method, headers=PREFLIGHT_HEADERS)
    assert resp.status_code == status
    _assert_cors(resp)


def test_plain_options_is_not_treated_as_preflight(client):
    """Without Access-Control-Request-Method, Flask's automatic OPTIONS answers."""
    resp = client.options("/v1/models")
    assert resp.status_code == HTTPStatus.OK
    assert "GET" in resp.headers["Allow"]
    _assert_cors(resp)


def test_non_v1_routes_get_no_cors(auth_client):
    resp = auth_client.get("/profile", headers={"Origin": ORIGIN})
    assert resp.status_code == HTTPStatus.OK
    assert not any(h.lower().startswith("access-control-") for h in resp.headers.keys())


def test_oauth_token_keeps_its_own_cors(client):
    resp = client.options("/oauth/token", headers=PREFLIGHT_HEADERS)
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert resp.headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type"
