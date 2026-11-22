"""The generation endpoints refuse a model whose kind does not fit the route.

An embedding model (``output_modalities: ["embedding"]``) gets a 400 from
chat, completions, messages and both audio routes, after the access check and
before anything goes upstream; /v1/embeddings and /v1/messages/count_tokens
do not check the marker. The audio routes also refuse any model whose
``input_modalities`` does not include ``"audio"`` (unset included).
"""
from http import HTTPStatus
from io import BytesIO

import pytest
from sqlalchemy import func, select

from lumen.blueprints.api import routes
from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.request_log import RequestLog
from lumen.services import model_cache
from tests.routes.test_api_audio import _FakeAudio, _grant_finite_pool, _live_state
from tests.routes.test_api_auth import (
    _capturing_openai,
    _make_model_blocked,
    _set_model_flag,
    api_key,
    fresh_rate_limit,
)

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "fresh_rate_limit"]

EMBEDDING_MESSAGE = "Model '{}' is an embedding model; call it through /v1/embeddings"
AUDIO_MESSAGE = "Model '{}' does not accept audio input"
ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01"}


@pytest.fixture(autouse=True)
def _own_rate_limit_budget(fresh_rate_limit):
    """These routes share the per-key generation bucket (#601); start every
    test with a full budget."""


def _upstream(monkeypatch):
    """Patch the upstream client; returns the list of create() calls."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise AssertionError("the upstream must not be called")

    _capturing_openai(monkeypatch, routes, create)
    return calls


def _mark(app, test_model, **flags):
    with app.app_context():
        _set_model_flag(app, test_model, **flags)
    # Written behind the app's back: refresh the snapshot as the editor would.
    model_cache.refresh(app)


def _chat(client, token, model, stream=False):
    return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                       json={"model": model, "stream": stream,
                             "messages": [{"role": "user", "content": "hi"}]})


def _completion(client, token, model):
    return client.post("/v1/completions", headers={"Authorization": f"Bearer {token}"},
                       json={"model": model, "prompt": "hi"})


def _messages(client, token, model, stream=False, path="/v1/messages"):
    return client.post(path, headers={"x-api-key": token, **ANTHROPIC_HEADERS},
                       json={"model": model, "max_tokens": 16, "stream": stream,
                             "messages": [{"role": "user", "content": "hi"}]})


def _audio(client, token, model, kind="transcriptions"):
    return client.post(f"/v1/audio/{kind}", headers={"Authorization": f"Bearer {token}"},
                       data={"model": model, "file": (BytesIO(b"fake-audio"), "a.flac")},
                       content_type="multipart/form-data")


CASES = {
    "chat": _chat,
    "chat-stream": lambda c, t, m: _chat(c, t, m, stream=True),
    "completions": _completion,
    "messages": _messages,
    "messages-stream": lambda c, t, m: _messages(c, t, m, stream=True),
    "transcriptions": lambda c, t, m: _audio(c, t, m, "transcriptions"),
    "translations": lambda c, t, m: _audio(c, t, m, "translations"),
}


def _balances():
    return sorted((b.entity_id, b.coins_left) for b in db.session.execute(select(EntityBalance)).scalars())


def _usage_rows():
    return db.session.execute(select(func.count()).select_from(RequestLog)).scalar_one()


@pytest.mark.parametrize("case", list(CASES))
def test_embedding_model_refused(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, case,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, output_modalities=["embedding"])
    calls = _upstream(monkeypatch)
    state = _live_state(monkeypatch)
    with app.app_context():
        before = _balances()

    resp = CASES[case](client, token, test_model["model_name"])

    assert resp.status_code == HTTPStatus.BAD_REQUEST
    body = resp.get_json()
    message = EMBEDDING_MESSAGE.format(test_model["model_name"])
    if case.startswith("messages"):
        assert body == {"type": "error",
                        "error": {"type": "invalid_request_error", "message": message}}
    else:
        assert body["error"] == {"message": message, "type": "invalid_request_error",
                                 "code": "model_not_supported"}
    assert calls == []
    assert state.admits == 0
    with app.app_context():
        assert _balances() == before
        assert _usage_rows() == 0


@pytest.mark.parametrize("kind", ["transcriptions", "translations"])
@pytest.mark.parametrize("flags", [
    pytest.param({"input_modalities": ["text"]}, id="text"),
    pytest.param({"input_modalities": ["text"], "audio_cost_per_hour": 0.36}, id="text-with-audio-rate"),
    pytest.param({"input_modalities": None}, id="unset"),
])
def test_non_audio_model_refused_on_audio_routes(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, kind, flags,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, **flags)
    calls = _upstream(monkeypatch)
    state = _live_state(monkeypatch)
    with app.app_context():
        before = _balances()

    resp = _audio(client, token, test_model["model_name"], kind)

    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == {
        "message": AUDIO_MESSAGE.format(test_model["model_name"]),
        "type": "invalid_request_error", "code": "model_not_supported",
    }
    assert calls == []
    assert state.admits == 0
    with app.app_context():
        assert _balances() == before
        assert _usage_rows() == 0


def test_embedding_message_wins_on_audio_routes(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """An embedding model without the audio marker gets the embedding message."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, output_modalities=["embedding"], input_modalities=["text"])
    _upstream(monkeypatch)

    resp = _audio(client, token, test_model["model_name"])

    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["message"] == EMBEDDING_MESSAGE.format(test_model["model_name"])


@pytest.mark.parametrize("modalities", [["audio"], ["text", "audio"]], ids=["audio", "text-and-audio"])
def test_audio_model_accepted(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, modalities,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, input_modalities=modalities)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return _FakeAudio()

    _capturing_openai(monkeypatch, routes, create)

    resp = _audio(client, token, test_model["model_name"])

    assert resp.status_code == HTTPStatus.OK
    assert len(calls) == 1


def test_blocked_non_audio_model_is_403_not_400(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
        _set_model_flag(app, test_model, input_modalities=["text"])
        _make_model_blocked(test_model["id"])
    model_cache.refresh(app)
    calls = _upstream(monkeypatch)

    resp = _audio(client, token, test_model["model_name"])

    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "audio input" not in resp.get_json()["error"]["message"]
    assert calls == []


def test_unknown_model_on_audio_route_is_404_not_400(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """No such model, while the configured one lacks the audio marker: still 404."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, input_modalities=["text"])
    calls = _upstream(monkeypatch)

    resp = _audio(client, token, "no-such-model")

    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert calls == []


@pytest.mark.parametrize("send", [
    pytest.param(_chat, id="chat"),
    pytest.param(_audio, id="transcriptions"),
])
def test_blocked_embedding_model_is_403_not_400(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, send,
):
    """Without access, the caller gets the generic 403 and never learns the type."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
        _set_model_flag(app, test_model, output_modalities=["embedding"])
        _make_model_blocked(test_model["id"])
    model_cache.refresh(app)
    calls = _upstream(monkeypatch)

    resp = send(client, token, test_model["model_name"])

    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "embedding" not in resp.get_json()["error"]["message"]
    assert calls == []


@pytest.mark.parametrize("send", [
    pytest.param(_chat, id="chat"),
    pytest.param(_audio, id="transcriptions"),
])
def test_unknown_model_is_404_not_400(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, send,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, output_modalities=["embedding"])
    calls = _upstream(monkeypatch)

    resp = send(client, token, "no-such-model")

    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert calls == []


def test_count_tokens_does_not_check_the_marker(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, output_modalities=["embedding"])

    resp = _messages(client, token, test_model["model_name"], path="/v1/messages/count_tokens")

    assert resp.status_code == HTTPStatus.OK
    assert "input_tokens" in resp.get_json()


def test_unknown_output_modalities_still_chat(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """``[]`` means unknown, not embedding: the chat call goes upstream."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _mark(app, test_model, output_modalities=[])
    calls = []

    class _Response:
        class usage:  # noqa: N801 - stands in for the OpenAI usage object
            prompt_tokens = 1
            completion_tokens = 1

        def model_dump(self, **_):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    def create(**kwargs):
        calls.append(kwargs)
        return _Response()

    _capturing_openai(monkeypatch, routes, create)

    resp = _chat(client, token, test_model["model_name"])

    assert resp.status_code == HTTPStatus.OK
    assert len(calls) == 1
