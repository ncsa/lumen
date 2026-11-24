"""Which /v1 routes share a per-key rate-limit bucket (#601, #756).

Chat, completions and Anthropic messages share one per-key bucket of
``rate_limiting.chat``. Embeddings and audio each have a per-key bucket of their
own, sized by ``rate_limiting.embedding`` / ``rate_limiting.audio`` or, when unset,
by the chat value; both audio routes share the one audio bucket. count_tokens and
acknowledge never call a model and keep a bucket of their own.
"""
import json
import logging
from http import HTTPStatus
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from lumen import extensions
from lumen.blueprints.api import routes
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.services.crypto import hash_api_key
from tests.routes.test_api_auth import _allow_model, api_key, fresh_rate_limit

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "fresh_rate_limit"]

ANTHROPIC = {"anthropic-version": "2023-06-01", "content-type": "application/json"}


class _Response:
    """One upstream reply that serves the chat, completions, audio and embeddings stubs."""

    class usage:  # noqa: N801 - stands in for the OpenAI usage object
        prompt_tokens = 1
        completion_tokens = 1

    def model_dump(self, **kwargs):
        return {"id": "x", "text": "hello", "choices": [{
            "index": 0, "text": "hello",
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def caller(app, client, test_user, audio_model, test_model_endpoint, api_key, monkeypatch,
           fresh_rate_limit):
    """A key with model access, an upstream stub, and a limit of 2 per minute."""
    _allow_model(app, test_user, audio_model)
    upstream = MagicMock()
    upstream.__enter__ = MagicMock(return_value=upstream)
    upstream.__exit__ = MagicMock(return_value=False)
    for create in (upstream.chat.completions.create, upstream.completions.create,
                   upstream.audio.transcriptions.create, upstream.audio.translations.create,
                   upstream.embeddings.create):
        create.return_value = _Response()
    monkeypatch.setattr(routes.openai, "OpenAI", MagicMock(return_value=upstream))
    monkeypatch.setattr(extensions, "_rate_limit_warned", set())
    _set_limits(app, monkeypatch, chat="2 per minute")
    token, _ = api_key
    return token


def _set_limits(app, monkeypatch, **rate_limiting):
    """Replace ``rate_limiting`` in YAML_DATA, as a hot reload would."""
    monkeypatch.setitem(app.config, "YAML_DATA",
                        {**app.config.get("YAML_DATA", {}), "rate_limiting": rate_limiting})


def _call(client, token, route):
    model = "test-model"
    if route == "chat":
        return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                           json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    if route == "completions":
        return client.post("/v1/completions", headers={"Authorization": f"Bearer {token}"},
                           json={"model": model, "prompt": "hi"})
    if route == "embeddings":
        return client.post("/v1/embeddings", headers={"Authorization": f"Bearer {token}"},
                           json={"model": model, "input": "hi"})
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
    ("messages", "chat", "completions"),
    ("transcriptions", "translations", "transcriptions"),
    ("translations", "transcriptions", "translations"),
    ("embeddings", "embeddings", "embeddings"),
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


def _spend(client, token, route, times=2):
    for _ in range(times):
        assert _call(client, token, route).status_code == HTTPStatus.OK
    assert _call(client, token, route).status_code == HTTPStatus.TOO_MANY_REQUESTS


@pytest.mark.parametrize("spent", ["chat", "embeddings", "transcriptions"])
def test_unset_categories_each_get_their_own_bucket_of_the_chat_size(client, caller, spent):
    """With embedding/audio unset, chat, embeddings and audio are three buckets of 2."""
    _spend(client, caller, spent)
    for route in {"chat", "embeddings", "transcriptions"} - {spent}:
        _spend(client, caller, route)


@pytest.mark.parametrize("category, routes_", [
    ("embedding", ("embeddings",)),
    ("audio", ("transcriptions", "translations")),
])
def test_explicit_category_limit_is_independent_of_chat(app, client, caller, monkeypatch, category, routes_):
    _set_limits(app, monkeypatch, chat="2 per minute", **{category: "3 per minute"})
    for i in range(3):
        assert _call(client, caller, routes_[i % len(routes_)]).status_code == HTTPStatus.OK
    for route in routes_:
        assert _call(client, caller, route).status_code == HTTPStatus.TOO_MANY_REQUESTS
    # The chat bucket is untouched, and spending it leaves the category spent, not reset.
    _spend(client, caller, "chat")


def test_spent_chat_leaves_explicit_category_budgets(app, client, caller, monkeypatch):
    _set_limits(app, monkeypatch, chat="2 per minute", embedding="3 per minute", audio="3 per minute")
    _spend(client, caller, "messages")
    _spend(client, caller, "embeddings", times=3)
    _spend(client, caller, "translations", times=3)


@pytest.mark.parametrize("route", ["embeddings", "transcriptions"])
def test_category_429_carries_retry_after_and_code(client, caller, route):
    for _ in range(2):
        _call(client, caller, route)
    resp = _call(client, caller, route)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert int(resp.headers["Retry-After"]) > 0
    assert resp.get_json()["error"]["code"] == "rate_limit_exceeded"


def test_hot_reload_changes_the_effective_limits(app, client, caller, monkeypatch):
    _spend(client, caller, "embeddings")
    # The limit is read per request. flask-limiter keys its counter on the limit
    # itself, so a changed value starts a fresh count at the new size.
    _set_limits(app, monkeypatch, chat="2 per minute", embedding="4 per minute")
    _spend(client, caller, "embeddings", times=4)
    _set_limits(app, monkeypatch, chat="1 per minute", embedding="4 per minute")
    _spend(client, caller, "chat", times=1)
    assert _call(client, caller, "embeddings").status_code == HTTPStatus.TOO_MANY_REQUESTS


@pytest.mark.parametrize("bad", ["lots per minute", "", 500])
def test_invalid_category_limit_warns_and_uses_the_chat_value(app, client, caller, monkeypatch, caplog, bad):
    _set_limits(app, monkeypatch, chat="2 per minute", embedding=bad)
    with caplog.at_level(logging.WARNING):
        _spend(client, caller, "embeddings")
    warnings = [r for r in caplog.records if "rate_limiting.embedding" in r.getMessage()]
    assert len(warnings) == 1
    assert "using the chat limit" in warnings[0].getMessage()
    # Its own bucket of the chat size, not the chat bucket.
    _spend(client, caller, "chat")


def test_legacy_limit_works_like_chat_and_warns_once(app, client, caller, monkeypatch, caplog):
    _set_limits(app, monkeypatch, limit="2 per minute")
    with caplog.at_level(logging.WARNING):
        _spend(client, caller, "chat")
        _spend(client, caller, "embeddings")
        _spend(client, caller, "transcriptions")
    warnings = [r for r in caplog.records if "rate_limiting.limit is deprecated" in r.getMessage()]
    assert len(warnings) == 1
    assert "rate_limiting.chat" in warnings[0].getMessage()
    assert "2.7.0" in warnings[0].getMessage()


def test_chat_key_drives_the_chat_ui_limit(app, monkeypatch):
    monkeypatch.setattr(extensions, "_rate_limit_warned", set())
    with app.app_context():
        _set_limits(app, monkeypatch, chat="7 per minute")
        assert extensions._chat_limit() == "7 per minute"
        _set_limits(app, monkeypatch, limit="8 per minute")
        assert extensions._chat_limit() == "8 per minute"
        _set_limits(app, monkeypatch)
        assert extensions._chat_limit() == "30 per minute"
