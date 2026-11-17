"""Which /v1 routes share a per-key rate-limit bucket (#601).

Chat, completions, Anthropic messages and audio all send a request upstream, so
one API key gets ``rate_limiting.limit`` once across them. count_tokens and
acknowledge never call a model and keep a bucket of their own.
"""
import json
from http import HTTPStatus
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from lumen.blueprints.api import routes
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.services.crypto import hash_api_key
from tests.routes.test_api_auth import _allow_model, api_key, fresh_rate_limit

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "fresh_rate_limit"]

ANTHROPIC = {"anthropic-version": "2023-06-01", "content-type": "application/json"}


class _Response:
    """One upstream reply that serves the chat, completions and audio stubs."""

    class usage:  # noqa: N801 - stands in for the OpenAI usage object
        prompt_tokens = 1
        completion_tokens = 1

    def model_dump(self):
        return {"id": "x", "text": "hello", "choices": [{
            "index": 0, "text": "hello",
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def caller(app, client, test_user, test_model, test_model_endpoint, api_key, monkeypatch,
           fresh_rate_limit):
    """A key with model access, an upstream stub, and a limit of 2 per minute."""
    _allow_model(app, test_user, test_model)
    upstream = MagicMock()
    upstream.__enter__ = MagicMock(return_value=upstream)
    upstream.__exit__ = MagicMock(return_value=False)
    for create in (upstream.chat.completions.create, upstream.completions.create,
                   upstream.audio.transcriptions.create, upstream.audio.translations.create):
        create.return_value = _Response()
    monkeypatch.setattr(routes.openai, "OpenAI", MagicMock(return_value=upstream))
    monkeypatch.setitem(app.config, "YAML_DATA",
                        {**app.config.get("YAML_DATA", {}), "rate_limiting": {"limit": "2 per minute"}})
    token, _ = api_key
    return token


def _call(client, token, route):
    model = "test-model"
    if route == "chat":
        return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                           json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    if route == "completions":
        return client.post("/v1/completions", headers={"Authorization": f"Bearer {token}"},
                           json={"model": model, "prompt": "hi"})
    if route in ("transcriptions", "translations"):
        return client.post(f"/v1/audio/{route}", headers={"Authorization": f"Bearer {token}"},
                           data={"model": model, "file": (BytesIO(b"audio"), "a.flac")},
                           content_type="multipart/form-data")
    path = "/v1/messages/count_tokens" if route == "count_tokens" else "/v1/messages"
    body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    return client.post(path, headers={"x-api-key": token, **ANTHROPIC}, data=json.dumps(body))


@pytest.mark.parametrize("first, second, third", [
    ("chat", "chat", "messages"),
    ("messages", "messages", "completions"),
    ("transcriptions", "chat", "messages"),
    ("chat", "translations", "transcriptions"),
])
def test_generating_routes_share_one_budget(client, caller, first, second, third):
    assert _call(client, caller, first).status_code == HTTPStatus.OK
    assert _call(client, caller, second).status_code == HTTPStatus.OK
    assert _call(client, caller, third).status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_count_tokens_keeps_its_own_budget(client, caller):
    for route in ("chat", "messages"):
        assert _call(client, caller, route).status_code == HTTPStatus.OK
    assert _call(client, caller, "chat").status_code == HTTPStatus.TOO_MANY_REQUESTS

    # The shared bucket is spent; count_tokens still has its own two calls.
    assert _call(client, caller, "count_tokens").status_code == HTTPStatus.OK
    assert _call(client, caller, "count_tokens").status_code == HTTPStatus.OK
    assert _call(client, caller, "count_tokens").status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_spent_count_tokens_leaves_generation_budget(client, caller):
    for _ in range(2):
        assert _call(client, caller, "count_tokens").status_code == HTTPStatus.OK
    assert _call(client, caller, "count_tokens").status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert _call(client, caller, "chat").status_code == HTTPStatus.OK


def test_two_keys_do_not_share_a_bucket(app, client, test_user, caller):
    other = "lk_test_token_other"
    with app.app_context():
        db.session.add(APIKey(entity_id=test_user["id"], name="other-key",
                              key_hash=hash_api_key(other)))
        db.session.commit()

    for _ in range(2):
        assert _call(client, caller, "chat").status_code == HTTPStatus.OK
    assert _call(client, caller, "messages").status_code == HTTPStatus.TOO_MANY_REQUESTS

    assert _call(client, other, "chat").status_code == HTTPStatus.OK
    assert _call(client, other, "messages").status_code == HTTPStatus.OK


def test_shared_bucket_429_carries_retry_after_and_code(client, caller):
    for _ in range(2):
        _call(client, caller, "messages")
    resp = _call(client, caller, "completions")
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert int(resp.headers["Retry-After"]) > 0
    assert resp.get_json()["error"]["code"] == "rate_limit_exceeded"
