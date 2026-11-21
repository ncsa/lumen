"""Tests for the /v1/embeddings endpoint."""
import logging
from http import HTTPStatus

import openai
import pytest
from openai.types import CreateEmbeddingResponse
from sqlalchemy import select

from lumen.blueprints.api import routes
from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.model_alias import ModelAlias
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services import model_cache
from tests.routes.test_api_audio import _exhaust_budget, _grant_finite_pool, _live_state
from tests.routes.test_api_auth import (
    _capturing_openai,
    _make_openai_error,
    _set_model_flag,
    fresh_rate_limit,
)

# Re-exported so pytest resolves the fixture by name in this module's tests.
__all__ = ["fresh_rate_limit"]


@pytest.fixture(autouse=True)
def _own_rate_limit_budget(fresh_rate_limit):
    """Embeddings share the per-key generation bucket with chat, messages and
    audio (#601); start every test here with a full budget."""


@pytest.fixture
def api_key(app, test_user):
    """Create an active API key for test_user. Returns (token, key_id)."""
    from lumen.models.api_key import APIKey
    from lumen.services.crypto import hash_api_key
    token = "lk_test_embeddings_token"
    with app.app_context():
        ak = APIKey(entity_id=test_user["id"], name="embeddings-key", key_hash=hash_api_key(token))
        db.session.add(ak)
        db.session.commit()
        return token, ak.id


def _response(n=1, embedding=(0.1, 0.2, 0.3), usage=True, prompt_tokens=1000):
    """A real SDK response object, as the upstream call would return it."""
    body = {
        "object": "list",
        "model": "upstream-name",
        "data": [{"object": "embedding", "index": i, "embedding": embedding} for i in range(n)],
    }
    if usage:
        body["usage"] = {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens}
    # construct, like the SDK: a base64 embedding is a str, not a list[float].
    return CreateEmbeddingResponse.construct(**body)


def _upstream(monkeypatch, result):
    """Patch the upstream client; returns (calls to create, OpenAI() kwargs)."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    clients = _capturing_openai(monkeypatch, routes, create)
    return calls, clients


def _post(client, token, body):
    return client.post("/v1/embeddings", headers={"Authorization": f"Bearer {token}"}, json=body)


def _log():
    return db.session.execute(select(RequestLog)).scalar_one()


# ---------------------------------------------------------------------------
# Auth and validation
# ---------------------------------------------------------------------------

def test_missing_auth_400(client):
    assert client.post("/v1/embeddings", json={"model": "test-model", "input": "hi"}).status_code \
        == HTTPStatus.BAD_REQUEST


def test_invalid_token_401(client):
    resp = _post(client, "not-real", {"model": "test-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("body, message", [
    (None, "Invalid request body"),
    ([1, 2], "Invalid request body"),
    ({"input": "hi"}, "model is required"),
    ({"model": 5, "input": "hi"}, "model is required"),
    ({"model": "test-model"}, "input is required"),
    ({"model": "test-model", "input": ""}, "input must be"),
    ({"model": "test-model", "input": []}, "input must be"),
    ({"model": "test-model", "input": None}, "input must be"),
    ({"model": "test-model", "input": 7}, "input must be"),
    ({"model": "test-model", "input": ["a", ""]}, "input must be"),
    ({"model": "test-model", "input": ["a", 1]}, "input must be"),
    ({"model": "test-model", "input": [True, False]}, "input must be"),
    ({"model": "test-model", "input": [[1, 2], []]}, "input must be"),
    ({"model": "test-model", "input": [[1, "x"]]}, "input must be"),
])
def test_invalid_request_400(client, api_key, monkeypatch, body, message):
    token, _ = api_key
    calls, _ = _upstream(monkeypatch, _response())
    if body is None:
        resp = client.post("/v1/embeddings", headers={"Authorization": f"Bearer {token}"},
                           data="not json", content_type="application/json")
    else:
        resp = _post(client, token, body)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    err = resp.get_json()["error"]
    assert err["type"] == "invalid_request_error"
    assert message in err["message"]
    assert calls == []


# ---------------------------------------------------------------------------
# Happy paths and billing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inp, n", [
    ("hello world", 1),
    (["one", "two", "three"], 3),
    ([1, 2, 3], 1),
    ([[1, 2], [3]], 2),
])
def test_embeds_and_bills_input_tokens(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, inp, n,
):
    token, ak_id = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        # The non-zero output price is deliberate: it is what catches any
        # output tokens being billed (#702).
        _set_model_flag(app, test_model, input_cost_per_million=3.0, output_cost_per_million=50.0)
        model_cache.refresh(app)
    calls, _ = _upstream(monkeypatch, _response(n=n, prompt_tokens=2000))

    resp = _post(client, token, {"model": "test-model", "input": inp})

    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    assert body["object"] == "list"
    assert body["model"] == "test-model"
    assert [d["embedding"] for d in body["data"]] == [[0.1, 0.2, 0.3]] * n
    assert body["usage"] == {"prompt_tokens": 2000, "total_tokens": 2000}
    assert calls[0]["input"] == inp

    # Input only, at the input price: the output price never applies.
    expected = round(2000 * 3.0 / 1_000_000, 6)
    with app.app_context():
        log = _log()
        assert log.input_tokens == 2000
        assert log.output_tokens == 0
        assert float(log.cost) == expected
        assert log.api_key_id == ak_id
        assert log.source == "api"
        assert log.outcome == "ok"
        bal = db.session.execute(select(EntityBalance)).scalar_one()
        assert float(bal.coins_left) == round(100 - expected, 6)


def test_alias_is_reported_and_canonical_name_forwarded(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """With no endpoint override, the upstream gets the canonical name, never the alias."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
        db.session.get(ModelEndpoint, test_model_endpoint["id"]).model_name = None
        db.session.add(ModelAlias(alias="embed-alias", model_config_id=test_model["id"]))
        db.session.commit()
        model_cache.refresh(app)
    calls, _ = _upstream(monkeypatch, _response())

    resp = _post(client, token, {"model": "embed-alias", "input": "hi"})

    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["model"] == "embed-alias"
    assert calls[0]["model"] == "test-model"


def test_forwards_only_allowlisted_fields(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    calls, clients = _upstream(monkeypatch, _response())

    resp = _post(client, token, {
        "model": "test-model", "input": "hi", "encoding_format": "base64",
        "dimensions": 256, "user": "u-1",
        "extra_body": {"model": "other"}, "extra_headers": {"X-A": "b"}, "priority": 9,
    })

    assert resp.status_code == HTTPStatus.OK
    # The endpoint's override name from the fixture, and nothing beyond the allowlist.
    assert calls == [{"model": "dummy", "input": "hi", "encoding_format": "base64",
                      "dimensions": 256, "user": "u-1"}]
    assert clients[0]["base_url"] == "http://localhost:9999/v1"


def test_encoding_format_defaults_to_float(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    calls, _ = _upstream(monkeypatch, _response())

    assert _post(client, token, {"model": "test-model", "input": "hi"}).status_code == HTTPStatus.OK
    assert calls == [{"model": "dummy", "input": "hi", "encoding_format": "float"}]


def test_base64_embeddings_are_passed_through(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A base64 vector is a string the SDK types as list[float]; it comes back as is."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    _upstream(monkeypatch, _response(embedding="zczMPc3MTD4="))

    resp = _post(client, token, {"model": "test-model", "input": "hi", "encoding_format": "base64"})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["data"][0]["embedding"] == "zczMPc3MTD4="


def test_no_usage_bills_zero_and_warns(
    app, client, monkeypatch, caplog, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
    _upstream(monkeypatch, _response(usage=False))

    with caplog.at_level(logging.WARNING, logger="lumen.blueprints.api.routes"):
        resp = _post(client, token, {"model": "test-model", "input": "hi"})

    assert resp.status_code == HTTPStatus.OK
    assert "Upstream did not return usage data" in caplog.text
    with app.app_context():
        log = _log()
        assert log.input_tokens == 0
        assert float(log.cost) == 0.0


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

def test_unknown_model_404(app, client, monkeypatch, test_user, api_key):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    calls, _ = _upstream(monkeypatch, _response())

    resp = _post(client, token, {"model": "no-such-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert calls == []


def test_unacked_model_403_consent_required(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
        _set_model_flag(app, test_model, needs_ack=True)
        model_cache.refresh(app)
    calls, _ = _upstream(monkeypatch, _response())

    resp = _post(client, token, {"model": "test-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["code"] == "consent_required"
    assert calls == []


def test_exhausted_budget_429(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10)
    state = _live_state(monkeypatch)
    calls, _ = _upstream(monkeypatch, _response())

    resp = _post(client, token, {"model": "test-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert resp.get_json()["error"]["code"] == "insufficient_quota"
    assert calls == []
    assert state.admits == 0


def test_upstream_4xx_is_passed_through(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    exc = _make_openai_error(openai.BadRequestError, HTTPStatus.BAD_REQUEST,
                             {"error": {"message": "input too long", "type": "invalid_request_error"}})
    _upstream(monkeypatch, exc)

    resp = _post(client, token, {"model": "test-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["message"] == "input too long"
    with app.app_context():
        assert db.session.execute(select(RequestLog)).first() is None


def test_upstream_failure_is_500_and_released(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    state = _live_state(monkeypatch)
    _upstream(monkeypatch, RuntimeError("upstream is down"))

    resp = _post(client, token, {"model": "test-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert resp.get_json()["error"]["type"] == "api_error"
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_admits_once_and_releases_once(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    state = _live_state(monkeypatch)
    observed = []

    def create(**kwargs):
        observed.append(state.inflight)
        return _response()

    _capturing_openai(monkeypatch, routes, create)

    assert _post(client, token, {"model": "test-model", "input": "hi"}).status_code == HTTPStatus.OK
    assert observed == [1], "the request was not in flight during the upstream call"
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_output_price_is_never_charged(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """Even an upstream that reports completion tokens is billed as input only."""
    token, _ = api_key
    with app.app_context():
        # test_model's output price (2 per million) is deliberately non-zero:
        # it is what catches any output tokens being billed (#702).
        _grant_finite_pool(app, test_user["id"])
    response = _response(prompt_tokens=10)
    response.usage.completion_tokens = 99
    _upstream(monkeypatch, response)

    assert _post(client, token, {"model": "test-model", "input": "hi"}).status_code == HTTPStatus.OK
    with app.app_context():
        log = _log()
        assert log.output_tokens == 0
        assert float(log.cost) == round(10 * 1.0 / 1_000_000, 6)
        assert db.session.get(ModelConfig, test_model["id"]).output_cost_per_million == 2


@pytest.mark.parametrize("in_rate, out_rate, in_tok, out_tok", [
    (1.0, 1000.0, 1000, 500),
    (0.02, 60.0, 37, 4096),
])
def test_high_output_price_is_never_charged(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    in_rate, out_rate, in_tok, out_tok,
):
    """An output price far above the input price, with completion tokens
    reported upstream, still bills input tokens at the input price only, so
    any output token billed would show up in the cost (#702)."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_model_flag(app, test_model, input_cost_per_million=in_rate,
                        output_cost_per_million=out_rate)
        model_cache.refresh(app)
    response = _response(prompt_tokens=in_tok)
    response.usage.completion_tokens = out_tok
    _upstream(monkeypatch, response)

    assert _post(client, token, {"model": "test-model", "input": "hi"}).status_code == HTTPStatus.OK
    expected = round(in_tok * in_rate / 1_000_000, 6)
    with app.app_context():
        log = _log()
        assert log.input_tokens == in_tok
        assert log.output_tokens == 0
        assert float(log.cost) == expected
        bal = db.session.execute(select(EntityBalance)).scalar_one()
        assert float(bal.coins_left) == pytest.approx(100 - expected)


def test_cached_tokens_are_billed_at_the_input_price(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A reported cache breakdown never discounts an embedding: with input and
    cache-read rates of 3 and 1 per million, 1000 tokens (500 cached) cost 0.003."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_model_flag(app, test_model, input_cost_per_million=3.0,
                        cached_input_cost_per_million=1.0, cache_write_cost_per_million=0.5)
        model_cache.refresh(app)
    response = CreateEmbeddingResponse.model_validate({
        "object": "list", "model": "upstream-name",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
        "usage": {"prompt_tokens": 1000, "total_tokens": 1000,
                  "prompt_tokens_details": {"cached_tokens": 500, "cache_creation_input_tokens": 200}},
    })
    _upstream(monkeypatch, response)

    assert _post(client, token, {"model": "test-model", "input": "hi"}).status_code == HTTPStatus.OK
    with app.app_context():
        log = _log()
        assert log.input_tokens == 1000
        assert log.cached_input_tokens is None
        assert log.cache_write_tokens is None
        assert float(log.cost) == 0.003
        bal = db.session.execute(select(EntityBalance)).scalar_one()
        assert float(bal.coins_left) == pytest.approx(100 - 0.003)


def test_embedding_model_is_listed_and_callable(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A model marked as an embedding model is hidden from chat, but /v1/models
    still lists it with its marker, and /v1/embeddings serves it."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
        _set_model_flag(app, test_model, input_modalities=["text"], output_modalities=["embedding"])
        model_cache.refresh(app)
    _upstream(monkeypatch, _response())

    listed = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).get_json()["data"]
    entry = next(m for m in listed if m["id"] == "test-model")
    assert entry["output_modalities"] == ["embedding"]

    resp = _post(client, token, {"model": "test-model", "input": "hi"})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["model"] == "test-model"
