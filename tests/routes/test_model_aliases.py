"""Route tests for model aliases over the API and chat surfaces.

An alias resolves to a canonical ModelConfig: discovery surfaces both, billing/
metrics attribute to the canonical model, and the client-facing ``model`` field
reports the name the client requested (which may be the alias).
"""

from http import HTTPStatus

import pytest
from sqlalchemy import select

from lumen.extensions import db


def _grant_unlimited_pool(app, entity_id):
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(
        entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0,
    ))
    db.session.commit()


@pytest.fixture
def api_key(app, test_user):
    """Create an active API key for test_user. Returns (token, key_id)."""
    token = "lk_test_token_abc123"
    with app.app_context():
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        ak = APIKey(
            entity_id=test_user["id"],
            name="test-key",
            key_hash=hash_api_key(token),
            active=True,
        )
        db.session.add(ak)
        db.session.commit()
        return token, ak.id


def _add_alias(app, alias, model_config_id):
    from lumen.models.model_alias import ModelAlias
    db.session.add(ModelAlias(alias=alias, model_config_id=model_config_id))
    db.session.commit()


def _request_log_ids(app):
    from lumen.models.request_log import RequestLog
    return [r.model_config_id for r in db.session.execute(select(RequestLog)).scalars().all()]


class _Usage:
    prompt_tokens = 5
    completion_tokens = 7


def test_list_models_includes_aliases(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        _add_alias(app, "alias-one", test_model["id"])
        _add_alias(app, "alias-two", test_model["id"])

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    ids = {m["id"] for m in resp.get_json()["data"]}
    assert test_model["model_name"] in ids
    assert "alias-one" in ids
    assert "alias-two" in ids


def test_list_models_alias_loading_does_not_scale_with_catalog(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    """Regression: discovery must not run one alias query per visible model.

    A 20-model catalog used to emit 20 separate ``SELECT ... FROM model_aliases``
    statements on every /v1/models request; alias loading must stay batched
    regardless of catalog size.
    """
    from sqlalchemy import event

    from lumen.extensions import db

    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        engine = db.engine

    def _create_models(n):
        with app.app_context():
            from lumen.models.model_config import ModelConfig
            for i in range(n):
                db.session.add(ModelConfig(
                    model_name=f"bulk-model-{i}",
                    input_cost_per_million=1.0,
                    output_cost_per_million=2.0,
                ))
            db.session.commit()

    def _statements_during_request():
        statements = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == HTTPStatus.OK
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        return statements

    # Warm-up: absorbs one-off first-request work (rate cache population) so the
    # two counted requests below differ only in catalog size.
    client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})

    small = _statements_during_request()
    _create_models(20)
    large = _statements_during_request()

    assert len(large) == len(small)
    assert sum("model_aliases" in s for s in large) <= 1
    ids = {m["id"] for m in client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).get_json()["data"]}
    assert {f"bulk-model-{i}" for i in range(20)} <= ids


def test_get_model_by_alias_returns_metadata_under_alias(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        _add_alias(app, "alias-one", test_model["id"])

    resp = client.get("/v1/models/alias-one", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["id"] == "alias-one"
    # The entry describes the canonical model's capabilities, not a separate model.
    assert data["object"] == "model"
    assert data["root"] == "dummy"


def test_get_model_unknown_alias_404(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])

    resp = client.get("/v1/models/nope", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_nonstreaming_alias_request_returns_alias_and_bills_canonical(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A non-streaming chat completion through an alias reports the alias in the
    response ``model`` and records usage under the canonical model_config_id."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        _add_alias(app, "aliased-model", test_model["id"])

    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chat(self):
            def _create(**kwargs):
                return ChatCompletion(
                    id="cmpl-x",
                    object="chat.completion",
                    choices=[Choice(index=0, message=ChatCompletionMessage(
                        role="assistant", content="hi"), finish_reason="stop")],
                    created=1,
                    system_fingerprint=None,
                    model="backend-served-model",  # what the upstream reported
                    usage=CompletionUsage(prompt_tokens=5, completion_tokens=7,
                                          total_tokens=12),
                )
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "aliased-model",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    # The requested alias is echoed back, not the backend's served-model id.
    assert body["model"] == "aliased-model"

    with app.app_context():
        assert _request_log_ids(app) == [test_model["id"]]


def test_streaming_alias_request_uses_canonical_remote_model_and_echoes_alias(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """Streaming chat through an alias forwards the CANONICAL name upstream
    (or the endpoint override) and echoes the requested alias in each chunk."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        _add_alias(app, "aliased-model", test_model["id"])

    captured = {}

    class _Chunk:
        def __init__(self, usage):
            self.usage = usage
            self.choices = []
            self.model = None  # set by the stream to the requested name

        def model_dump(self):
            return {"model": self.model, "choices": [{"delta": {"content": "hi"}}]}

    def _stream():
        yield _Chunk(None)
        yield _Chunk(_Usage())

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chat(self):
            def _create(**kwargs):
                captured["remote_model"] = kwargs.get("model")
                return _stream()
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "aliased-model",
              "messages": [{"role": "user", "content": "hi"}],
              "stream": True},
    )
    body = resp.get_data(as_text=True)
    assert "data: [DONE]" in body
    # Never forward the alias upstream: the endpoint override ("dummy") is used.
    assert captured["remote_model"] == "dummy"
    # The requested alias is echoed back in each chunk.
    assert '"model": "aliased-model"' in body
    # Billing under the canonical model_config_id.
    with app.app_context():
        assert _request_log_ids(app) == [test_model["id"]]


def test_aliased_request_forwards_canonical_name_when_no_endpoint_override(
    app, client, monkeypatch, test_user, test_model, api_key,
):
    """When the endpoint has no ``model_name`` override, an alias request forwards
    the CANONICAL model name upstream, never the alias itself."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        _add_alias(app, "aliased-model", test_model["id"])
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="test-api-key-123",
            # No model_name — remote_model must fall back to the canonical name.
            healthy=True,
        ))
        db.session.commit()

    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    captured = {}

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chat(self):
            def _create(**kwargs):
                captured["remote_model"] = kwargs.get("model")
                return ChatCompletion(
                    id="cmpl-x", object="chat.completion",
                    choices=[Choice(index=0, message=ChatCompletionMessage(
                        role="assistant", content="hi"), finish_reason="stop")],
                    created=1, system_fingerprint=None,
                    model="back-end", usage=CompletionUsage(
                        prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "aliased-model",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.OK
    # The canonical name (not the alias) is forwarded upstream.
    assert captured["remote_model"] == test_model["model_name"]
    # The requested alias is echoed back to the client.
    assert resp.get_json()["model"] == "aliased-model"


def test_model_detail_page_shows_alias_badges(app, auth_client, test_model):
    with app.app_context():
        _add_alias(app, "old-name", test_model["id"])

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    html = resp.get_data(as_text=True)
    # Aliases are shown as metadata, not as duplicate model entries.
    assert "Also known as" in html
    assert "old-name" in html


def test_disabled_canonical_target_is_not_surfaced_through_alias(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        _add_alias(app, "old-name", test_model["id"])
        from lumen.models.model_config import ModelConfig
        db.session.get(ModelConfig, test_model["id"]).disabled = True
        db.session.commit()

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    ids = {(m["id"]) for m in resp.get_json()["data"]}
    assert test_model["model_name"] not in ids
    assert "old-name" not in ids

    resp = client.get("/v1/models/old-name", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.NOT_FOUND
