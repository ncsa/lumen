"""Tests for the /v1 API key authentication decorator (api_key_required)."""
from http import HTTPStatus

import pytest


@pytest.fixture
def api_key(app, test_user):
    """Create an active API key for test_user. Returns (token, key_id)."""
    token = "lk_test_token_abc123"
    with app.app_context():
        from lumen.extensions import db
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


@pytest.fixture
def inactive_api_key(app, test_user):
    token = "lk_test_inactive"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        ak = APIKey(
            entity_id=test_user["id"],
            name="inactive-key",
            key_hash=hash_api_key(token),
            active=False,
        )
        db.session.add(ak)
        db.session.commit()
        return token


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

def test_missing_authorization_header_400(client, test_model):
    resp = client.get(f"/v1/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    body = resp.get_json()
    assert body["error"]["type"] == "invalid_request_error"


def test_non_bearer_scheme_400(client, test_model):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_empty_bearer_token_400(client, test_model):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def test_unknown_token_401(client, test_model):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.get_json()["error"]["type"] == "authentication_error"


def test_inactive_api_key_401(client, test_model, inactive_api_key):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": f"Bearer {inactive_api_key}"},
    )
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


def test_inactive_entity_403(app, client, test_user, api_key):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.get(Entity, test_user["id"])
        entity.active = False
        db.session.commit()

    resp = client.get(
        "/v1/models/test-model",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# Valid key happy path
# ---------------------------------------------------------------------------

def _grant_unlimited_pool(app, entity_id):
    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(
        entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0,
    ))
    db.session.commit()


def test_valid_key_lists_accessible_model(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])

    resp = client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert test_model["model_name"] in ids


def test_valid_key_filters_blocked_model(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blocked",
        ))
        db.session.commit()

    resp = client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTPStatus.OK
    ids = [m["id"] for m in resp.get_json()["data"]]
    assert test_model["model_name"] not in ids


def test_get_model_blocked_returns_404(
    app, client, test_user, test_model, api_key,
):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blocked",
        ))
        db.session.commit()

    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_get_model_unknown_returns_404(client, api_key):
    token, _ = api_key
    resp = client.get(
        "/v1/models/does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Monitor token
# ---------------------------------------------------------------------------

def test_monitor_token_can_list_models(app, client, test_model):
    monitor = "monitor-secret-token"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "api": {"monitoring": {"token": monitor}}}
    try:
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {monitor}"})
        assert resp.status_code == HTTPStatus.OK
        # Monitor sees all models, not filtered by entity access
        ids = [m["id"] for m in resp.get_json()["data"]]
        assert test_model["model_name"] in ids
    finally:
        app.config["YAML_DATA"].pop("api", None)


def test_monitor_token_can_get_model(app, client, test_model):
    monitor = "monitor-secret-token-2"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "api": {"monitoring": {"token": monitor}}}
    try:
        resp = client.get(
            f"/v1/models/{test_model['model_name']}",
            headers={"Authorization": f"Bearer {monitor}"},
        )
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json()["id"] == test_model["model_name"]
    finally:
        app.config["YAML_DATA"].pop("api", None)


def test_monitor_token_blocked_from_chat_completions(app, client):
    monitor = "monitor-secret-token-3"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "api": {"monitoring": {"token": monitor}}}
    try:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {monitor}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == HTTPStatus.FORBIDDEN
        assert resp.get_json()["error"]["type"] == "authentication_error"
    finally:
        app.config["YAML_DATA"].pop("api", None)


# ---------------------------------------------------------------------------
# Chat completions request validation (cheap, no upstream call)
# ---------------------------------------------------------------------------

def test_chat_completions_missing_body_400(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data="",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_completions_wrong_content_type_json_error(client, api_key):
    """A body sent with a non-JSON Content-Type returns the JSON error, not a 415 HTML page."""
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
        data="model=test-model",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["type"] == "invalid_request_error"


def test_chat_completions_malformed_json_error(client, api_key):
    """Malformed JSON returns the JSON error, not a 400 HTML page from Werkzeug."""
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data="{not valid json",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["type"] == "invalid_request_error"


def test_chat_completions_missing_model_400(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_chat_completions_unknown_model_404(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_chat_completions_no_healthy_endpoint_503(
    app, client, test_user, test_model, api_key,
):
    """Model exists and user has access, but no healthy endpoint → 503."""
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.get_json()["error"]["type"] == "server_error"


def test_chat_completions_no_access_403(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blocked",
        ))
        db.session.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_chat_completions_graylist_no_consent_403(
    app, client, test_user, test_model, api_key,
):
    token, _ = api_key
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

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_chat_completions_graylist_with_consent_passes_access(
    app, client, test_user, test_model, api_key,
):
    """Graylist + consent clears the access gate (fails later at endpoint, not at 403)."""
    token, _ = api_key
    with app.app_context():
        from datetime import datetime, timezone

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

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code != HTTPStatus.FORBIDDEN


def test_chat_completions_whitelist_passes_access(
    app, client, test_user, test_model, api_key,
):
    """Whitelist clears the access gate (fails later at endpoint, not at 403)."""
    token, _ = api_key
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

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code != HTTPStatus.FORBIDDEN


def test_chat_completions_missing_messages_400(client, api_key):
    """model provided but messages omitted → 400."""
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "test-model"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["type"] == "invalid_request_error"


# ---------------------------------------------------------------------------
# api.consent flag — consent: false exempts API from graylist gate
# ---------------------------------------------------------------------------

def _set_api_consent(app, value: bool):
    yaml = dict(app.config.get("YAML_DATA") or {})
    yaml["api"] = {**yaml.get("api", {}), "consent": value}
    app.config["YAML_DATA"] = yaml
    app.config["API_REQUIRE_MODEL_CONSENT"] = value


def test_consent_false_graylist_chat_completions_passes_access(
    app, client, test_user, test_model, api_key,
):
    """api.consent=false: graylist model clears the access gate (fails later at endpoint, not at 403)."""
    token, _ = api_key
    _set_api_consent(app, False)
    try:
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

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": test_model["model_name"],
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code != HTTPStatus.FORBIDDEN
    finally:
        _set_api_consent(app, True)


def test_consent_false_graylist_list_models_includes_model(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    """api.consent=false: graylist model without consent appears in /v1/models."""
    token, _ = api_key
    _set_api_consent(app, False)
    try:
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

        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == HTTPStatus.OK
        ids = [m["id"] for m in resp.get_json()["data"]]
        assert test_model["model_name"] in ids
    finally:
        _set_api_consent(app, True)


def test_consent_false_graylist_get_model_returns_model(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    """api.consent=false: GET /v1/models/<id> returns graylist model without consent."""
    token, _ = api_key
    _set_api_consent(app, False)
    try:
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

        resp = client.get(
            f"/v1/models/{test_model['model_name']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json()["id"] == test_model["model_name"]
    finally:
        _set_api_consent(app, True)


def test_consent_true_graylist_chat_completions_403(
    app, client, test_user, test_model, api_key,
):
    """api.consent=true (explicit): graylist model without consent still returns 403."""
    token, _ = api_key
    _set_api_consent(app, True)
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

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_consent_false_blacklist_still_blocked(
    app, client, test_user, test_model, api_key,
):
    """api.consent=false never bypasses a hard blacklist block."""
    token, _ = api_key
    _set_api_consent(app, False)
    try:
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

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": test_model["model_name"],
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == HTTPStatus.FORBIDDEN
    finally:
        _set_api_consent(app, True)


def test_list_models_response_includes_required_openai_fields(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    """Each model in /v1/models must carry the OpenAI-spec required fields:
    id, object='model', created (int), owned_by (str).
    See: https://platform.openai.com/docs/api-reference/models/object
    """
    token, _ = api_key
    with app.app_context():
        _grant_unlimited_pool(app, test_user["id"])

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()["data"]
    assert data, "Expected at least one model in the list"
    for m in data:
        assert "id" in m
        assert m.get("object") == "model"
        assert isinstance(m.get("created"), int)
        assert isinstance(m.get("owned_by"), str)


# ---------------------------------------------------------------------------
# Upstream error pass-through
# ---------------------------------------------------------------------------
def _make_openai_error(error_cls, status, body):
    import httpx2
    req = httpx2.Request("POST", "http://upstream/v1/chat/completions")
    return error_cls(body.get("message", "error"),
                     response=httpx2.Response(status, request=req), body=body)


@pytest.mark.parametrize("body, expected_msg, expected_type", [
    # vLLM flat shape (the prod context-length error).
    ({"object": "error",
      "message": "Input length (48874 tokens) exceeds the maximum allowed length (48712 tokens).",
      "type": "BadRequestError", "code": 400},
     "Input length (48874 tokens) exceeds the maximum allowed length (48712 tokens).",
     "BadRequestError"),
    # OpenAI nested shape.
    ({"error": {"message": "too long", "type": "invalid_request_error"}},
     "too long", "invalid_request_error"),
])
def test_chat_completions_upstream_4xx_passes_through(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    body, expected_msg, expected_type,
):
    """A 4xx from the upstream (e.g. context-length exceeded) is the caller's
    mistake: surface the real status and message, not a generic 500 retry."""
    import openai

    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"], model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()

    exc = _make_openai_error(openai.BadRequestError, 400, body)

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        @property
        def chat(self):
            def _create(**kwargs):
                raise exc
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    err = resp.get_json()["error"]
    assert err["message"] == expected_msg
    assert err["type"] == expected_type


def test_chat_completions_upstream_5xx_is_generic_500(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A genuine upstream/transport failure stays a generic 500 retry message."""
    import openai

    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"], model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()

    exc = _make_openai_error(
        openai.InternalServerError, 500, {"message": "backend exploded"})

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        @property
        def chat(self):
            def _create(**kwargs):
                raise exc
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    err = resp.get_json()["error"]
    assert err["message"] == "Upstream error. Please try again."
    assert "backend exploded" not in err["message"]


def test_chat_completions_streaming_error_emits_done(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A mid-stream upstream error still terminates the SSE stream with [DONE]."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"], model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()

    class _Chunk:
        usage = None
        def model_dump(self):
            return {"choices": [{"delta": {"content": "hi"}}]}

    def _stream():
        yield _Chunk()
        raise RuntimeError("upstream blew up mid-stream")

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        @property
        def chat(self):
            def _create(**kwargs):
                return _stream()
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    body = resp.get_data(as_text=True)
    assert '"error"' in body
    assert "data: [DONE]" in body


def test_chat_completions_streaming_records_duration(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A successful streaming /v1/chat/completions request records a non-zero
    duration in request_logs, consistent with the chat streaming path
    (send_message_stream in llm.py) which has always recorded duration."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"], model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()

    class _Usage:
        prompt_tokens = 5
        completion_tokens = 7

    class _Chunk:
        def __init__(self, usage):
            self.usage = usage

        def model_dump(self):
            return {"choices": [{"delta": {"content": "hi"}}]}

    def _stream():
        yield _Chunk(None)
        yield _Chunk(_Usage())

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        @property
        def chat(self):
            def _create(**kwargs):
                return _stream()
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    body = resp.get_data(as_text=True)
    assert "data: [DONE]" in body

    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
        assert log.source == "api"
        assert log.input_tokens == 5
        assert log.output_tokens == 7
        assert log.duration > 0
        assert log.aborted is False  # a completed stream is not an abort


def _allow_model(app, test_user, test_model):
    """Grant the test user unlimited access to the test model."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"], model_config_id=test_model["id"],
            access_type="allowed",
        ))
        db.session.commit()


class _UsageChunk:
    """A final chunk carrying usage, so the generator reaches its billing block."""

    class usage:  # noqa: N801 - stands in for the OpenAI usage object
        prompt_tokens = 1
        completion_tokens = 1

    def model_dump(self):
        return {"choices": [{"delta": {"content": "hi"}}]}


def _fake_openai(monkeypatch, routes, chunks):
    def _stream():
        yield from chunks

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        @property
        def chat(self):
            def _create(**kwargs):
                return _stream()
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _FakeClient())


def test_streaming_error_after_billing_holds_no_connection(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """The error-path yields must not run while a connection is checked out.

    Billing checks a connection back out mid-generator, so an error raised after it
    left the connection held across the two error events. A client that has gone
    away never lets those yields finish, so the connection stayed checked out for
    the life of the process.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    with app.app_context():
        from lumen.extensions import db
        pool = db.engine.pool

    def boom(*a, **k):
        raise RuntimeError("billing blew up")

    _fake_openai(monkeypatch, routes, [_UsageChunk()])
    monkeypatch.setattr(routes, "_record_api_key_usage", boom)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == HTTPStatus.OK
    # Laziness is required: a buffered response would already have run teardown
    # and hidden any leak.
    assert resp.is_streamed

    saw_error = False
    try:
        for raw in resp.response:
            if b'"error"' in raw:
                saw_error = True
                assert pool.checkedout() == 0, (
                    "DB connection checked out while the error event is in flight"
                )
        assert saw_error
    finally:
        resp.close()


def test_streaming_billing_error_is_not_reported_as_an_upstream_error(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A failed commit is not the endpoint's fault, and must not be blamed on it.

    The stream ran to completion and the client already holds every chunk; only
    the accounting failed. The abort metric keeps the two apart via ``phase``,
    but the error event and the log line went through the upstream classifier,
    so both named an endpoint and a model that had done nothing wrong -- an
    operator following either goes hunting a backend problem that does not
    exist.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)

    def boom(*a, **k):
        raise RuntimeError("billing blew up")

    _fake_openai(monkeypatch, routes, [_UsageChunk()])
    monkeypatch.setattr(routes, "_record_api_key_usage", boom)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == HTTPStatus.OK
    try:
        body = b"".join(resp.response)
    finally:
        resp.close()

    assert b'"error"' in body, "the billing failure was not reported to the client at all"
    assert b"Upstream error" not in body, (
        "a billing failure was reported to the client as an upstream failure"
    )


def test_streaming_abandoned_by_client_releases_connection(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """Closing the stream early must not leave a connection checked out.

    The GeneratorExit path logs an aborted request, which checks a connection back
    out after the session was released before the LLM call. Note this passes with or
    without the generator's own ``finally``: Werkzeug always closes the iterable, so
    the app-context teardown covers this case. The ``finally`` is there for the case
    that cannot be reproduced in-process — an iterable abandoned without close, which
    is what leaves the context unpopped and the connection held for good.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    with app.app_context():
        from lumen.extensions import db
        pool = db.engine.pool

    _fake_openai(monkeypatch, routes, [_UsageChunk(), _UsageChunk()])

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.is_streamed
    next(iter(resp.response))  # one event, then walk away mid-stream
    resp.close()

    assert pool.checkedout() == 0


def test_streaming_abort_closes_upstream_before_logging(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """The upstream client closes — aborting the backend — before the abort log.

    Exiting ``with openai.OpenAI(...)`` aborts the upstream generation promptly.
    While that ``with`` sat outside the ``try``, the ``except GeneratorExit``
    handler's DB round-trip ran first and the backend kept generating for its
    whole duration.
    """
    from lumen.blueprints.api import routes
    from lumen.services import llm as llm_service
    token, _ = api_key
    _allow_model(app, test_user, test_model)

    events = []

    def _stream():
        yield _UsageChunk()
        yield _UsageChunk()

    class _ClosingClient:
        def __enter__(self): return self

        def __exit__(self, *a):
            events.append("upstream closed")
            return False

        @property
        def chat(self):
            def _create(**kwargs):
                return _stream()
            return type("C", (), {"completions": type("X", (), {"create": staticmethod(_create)})()})()

    monkeypatch.setattr(routes.openai, "OpenAI", lambda *a, **k: _ClosingClient())

    real_record = llm_service.record_aborted_request

    def _spy(*a, **k):
        events.append("abort logged")
        return real_record(*a, **k)

    monkeypatch.setattr(llm_service, "record_aborted_request", _spy)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.is_streamed
    next(iter(resp.response))  # one event, then walk away mid-stream
    resp.close()

    assert events == ["upstream closed", "abort logged"]

    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        logs = db.session.execute(
            select(RequestLog).filter_by(entity_id=test_user["id"])
        ).scalars().all()
    assert len(logs) == 1
    # The first chunk already carried usage, so the abort is billed exactly —
    # 1 input @ $1/M + 1 output @ $2/M — rather than estimated.
    assert logs[0].aborted is True
    assert (logs[0].input_tokens, logs[0].output_tokens) == (1, 1)
    assert float(logs[0].cost) == pytest.approx(0.000003)


class _ContentChunk:
    """A content-delta chunk with no usage — the shape an abort must estimate from."""

    usage = None

    def __init__(self, text="hi"):
        self.text = text
        self.choices = [type("Choice", (), {"delta": type("Delta", (), {"content": text})()})()]

    def model_dump(self):
        return {"choices": [{"delta": {"content": self.text}}]}


def test_streaming_disconnect_bills_estimated_usage(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
):
    """A client that hangs up mid-stream is billed for what the backend produced.

    No usage chunk arrived, so the counts are estimates: the prompt from its
    character count, the output as one token per content delta delivered. A
    zero-cost row here (the old behaviour) meant anyone could stream, disconnect
    before the terminal chunk, and pay nothing — repeatably.
    """
    import threading

    from sqlalchemy import select

    from lumen.blueprints.api import routes
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.models.entity_model_access import EntityModelAccess
    from lumen.models.request_log import RequestLog

    token, key_id = api_key
    with app.app_context():
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=10, refresh_coins=0, starting_coins=10,
        ))
        db.session.add(EntityBalance(entity_id=test_user["id"], coins_left=10))
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"], model_config_id=test_model["id"], access_type="allowed",
        ))
        db.session.commit()

    disconnected = threading.Event()
    monkeypatch.setattr(routes, "client_disconnect_event", lambda: disconnected)
    _fake_openai(monkeypatch, routes, [_ContentChunk(), _ContentChunk(), _ContentChunk()])

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "x" * 400}], "stream": True},
    )
    assert resp.is_streamed
    events = iter(resp.response)
    next(events)  # one content delta delivered
    disconnected.set()
    assert list(events) == []  # the stream stops itself — no [DONE]
    resp.close()

    with app.app_context():
        log = db.session.execute(
            select(RequestLog).filter_by(entity_id=test_user["id"])
        ).scalar_one()
        assert log.aborted is True
        assert log.input_tokens == 100  # 400 prompt characters / 4
        assert log.output_tokens == 1   # one content delta made it out
        # 100 input @ $1/M + 1 output @ $2/M
        assert float(log.cost) == pytest.approx(0.000102)
        balance = db.session.execute(
            select(EntityBalance).filter_by(entity_id=test_user["id"])
        ).scalar_one()
        assert float(balance.coins_left) == pytest.approx(10 - 0.000102)
        # the per-key totals move too, exactly as on the completed path
        key = db.session.get(APIKey, key_id)
        assert (key.input_tokens, key.output_tokens) == (100, 1)
        assert float(key.cost) == pytest.approx(0.000102)


# ---------------------------------------------------------------------------
# Upstream call bounds — every proxy client the API path builds must carry a
# timeout, and the streaming one must never auto-retry. Unbounded, the SDK's
# own defaults (600 s, two retries) let one stalled backend pin a WSGI worker
# thread for ~30 minutes on a single client request.
#
# The audio case is exercised here rather than in test_api_audio.py so all four
# proxy clients are covered in one place alongside the chat ones.
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_rate_limit(app):
    """Give this test the full per-key rate-limit budget, and leave it clean.

    The /v1 limiter is keyed on the API key's id over in-memory storage, and
    ``clean_db`` lets SQLite hand out id 1 again for every test's key — so the
    whole suite shares one 30-per-minute bucket. Tests that add API calls tip
    unrelated later tests into 429 unless they reset it; that is exactly how
    adding the tests below first surfaced.
    """
    from lumen.extensions import limiter
    with app.app_context():
        limiter.reset()
    yield
    with app.app_context():
        limiter.reset()


def _capturing_openai(monkeypatch, routes, create):
    """Patch openai.OpenAI and return the list of kwargs each construction got."""
    calls = []

    class _FakeClient:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        @property
        def chat(self):
            return type("C", (), {
                "completions": type("X", (), {"create": staticmethod(create)})(),
            })()

        @property
        def audio(self):
            return type("A", (), {
                "transcriptions": type("T", (), {"create": staticmethod(create)})(),
                "translations": type("R", (), {"create": staticmethod(create)})(),
            })()

    def _factory(*a, **kw):
        calls.append(kw)
        return _FakeClient()

    monkeypatch.setattr(routes.openai, "OpenAI", _factory)
    return calls


class _NonStreamResponse:
    """The shape _complete_and_bill expects back from a non-streaming call."""

    class usage:  # noqa: N801 - stands in for the OpenAI usage object
        prompt_tokens = 1
        completion_tokens = 1

    def model_dump(self):
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}


def _chat_post(client, token, model_name, stream):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": model_name,
              "messages": [{"role": "user", "content": "hi"}], "stream": stream},
    )


def test_non_streaming_chat_client_is_bounded(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """connect and read bounded separately; retries allowed — this attempt is
    finished and nothing has been sent to the client yet."""
    import openai

    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    calls = _capturing_openai(monkeypatch, routes, lambda **kw: _NonStreamResponse())

    assert _chat_post(client, token, test_model["model_name"], False).status_code == HTTPStatus.OK
    assert len(calls) == 1
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, openai.Timeout)  # openai.Timeout IS httpx.Timeout
    # read/write use LLM_REQUEST_TIMEOUT here, not the streaming inter-chunk
    # bound: with no chunks to pace it, this caps the whole generation.
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5.0, 600.0, 600.0, 5.0)
    assert calls[0]["max_retries"] == 1


def test_non_streaming_chat_bounds_come_from_config(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    monkeypatch.setitem(app.config, "LLM_CONNECT_TIMEOUT", 2.5)
    monkeypatch.setitem(app.config, "LLM_REQUEST_TIMEOUT", 33.0)
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 3)
    calls = _capturing_openai(monkeypatch, routes, lambda **kw: _NonStreamResponse())

    _chat_post(client, token, test_model["model_name"], False)
    assert (calls[0]["timeout"].connect, calls[0]["timeout"].read) == (2.5, 33.0)
    assert calls[0]["max_retries"] == 3


def test_streaming_chat_client_is_bounded_and_never_retries(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """A retried stream restarts the whole generation while the first attempt may
    still be draining upstream — two backend generations for one client request,
    with chunks already sent that cannot be un-sent."""
    import openai

    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    # Even with retries configured, the streaming client must ask for none.
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 4)
    calls = _capturing_openai(monkeypatch, routes, lambda **kw: iter([_UsageChunk()]))

    body = _chat_post(client, token, test_model["model_name"], True).get_data(as_text=True)
    assert "data: [DONE]" in body
    assert len(calls) == 1
    assert isinstance(calls[0]["timeout"], openai.Timeout)
    assert calls[0]["timeout"].read == 300.0
    assert calls[0]["max_retries"] == 0


def test_streaming_bounds_are_captured_into_the_generator_closure():
    """The bounds must be read in the view and closed over, not read inside
    ``generate()``.

    The response generator runs after the request's contexts are gone, on
    whatever worker thread iterates the body: no ``current_app``, so a lazy
    ``upstream_call_bounds()`` there is a 500 on every stream in production.
    Nothing in this suite can catch that at runtime — the session-scoped ``app``
    fixture holds an app context for the whole run, and Flask's test client
    preserves the request context across the body iteration too, so a misplaced
    read still finds both. So assert the structure instead, the same way
    ``test_no_stream_with_context.py`` does: ``timeout`` must be a free variable
    of ``generate`` (bound in the enclosing view) and not one of its locals.
    """
    from lumen.blueprints.api import routes

    generate = next(
        c for c in routes._do_chat.__code__.co_consts
        if getattr(c, "co_name", None) == "generate"
    )
    assert "timeout" in generate.co_freevars, (
        "the streaming client's timeout is not captured from the view — if it is "
        "read inside generate(), there is no current_app there in production"
    )
    assert "timeout" not in generate.co_varnames


def test_audio_client_is_bounded(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from io import BytesIO

    import openai

    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    calls = _capturing_openai(
        monkeypatch, routes,
        lambda **kw: type("R", (), {"model_dump": lambda self: {"text": "hi"}})(),
    )

    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        data={"model": test_model["model_name"], "file": (BytesIO(b"audio"), "a.flac")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == HTTPStatus.OK
    assert len(calls) == 1
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, openai.Timeout)
    # write bounds the upload of the audio file, read the transcription itself.
    # Both use LLM_REQUEST_TIMEOUT: transcribing a large file has no chunks to
    # pace it and legitimately takes minutes.
    assert (timeout.connect, timeout.read, timeout.write) == (5.0, 600.0, 600.0)
    assert calls[0]["max_retries"] == 1


# ---------------------------------------------------------------------------
# lumen_stream_aborts_total, API side. F1's lesson was that a monitoring hook
# nobody proves fires is worse than none, so these assert the increment.
# ---------------------------------------------------------------------------

def _abort_count(source, reason):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(
        "lumen_stream_aborts_total", {"source": source, "reason": reason}) or 0.0


def test_api_stream_disconnect_increments_the_abort_counter(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    import threading

    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    disconnected = threading.Event()
    monkeypatch.setattr(routes, "client_disconnect_event", lambda: disconnected)
    _fake_openai(monkeypatch, routes, [_ContentChunk(), _ContentChunk(), _UsageChunk()])
    before = _abort_count("api", "disconnect")
    before_err = _abort_count("api", "upstream_error")

    resp = _chat_post(client, token, test_model["model_name"], True)
    assert resp.is_streamed
    events = iter(resp.response)
    next(events)
    disconnected.set()
    assert list(events) == []
    resp.close()

    assert _abort_count("api", "disconnect") == before + 1
    # the other reason is untouched — the label really discriminates
    assert _abort_count("api", "upstream_error") == before_err


def test_api_stream_upstream_error_increments_its_own_reason(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """A broken backend must not look like clients hanging up."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)

    def _boom():
        yield _ContentChunk()
        raise RuntimeError("upstream blew up mid-stream")

    _capturing_openai(monkeypatch, routes, lambda **kw: _boom())
    before_err = _abort_count("api", "upstream_error")
    before_disc = _abort_count("api", "disconnect")

    body = _chat_post(client, token, test_model["model_name"], True).get_data(as_text=True)
    assert '"error"' in body

    assert _abort_count("api", "upstream_error") == before_err + 1
    assert _abort_count("api", "disconnect") == before_disc


def test_completed_api_stream_is_not_counted_as_an_abort(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _fake_openai(monkeypatch, routes, [_UsageChunk()])
    before = _abort_count("api", "disconnect")

    body = _chat_post(client, token, test_model["model_name"], True).get_data(as_text=True)
    assert "data: [DONE]" in body
    assert _abort_count("api", "disconnect") == before


# ---------------------------------------------------------------------------
# Request timing columns
#
# started_at and queue_wait come from marks the ASGI bridge publishes in the
# WSGI environ, and the Flask test client never goes through the bridge — so it
# is supplied here. What the test client *does* exercise for real is the
# before_request hook that derives queue_wait, and the whole path from the view
# (where the environ is readable) into the context-free response generator
# (where it is not).
# ---------------------------------------------------------------------------

_MAX_PLAUSIBLE_SPAN = 60 * 60  # seconds; a test request takes milliseconds
_QUEUE_WAIT = 0.05             # seconds of admission wait to stamp T0 behind
_SEND_BLOCKED = 0.25           # seconds the responder reports blocked in send


def _bridge_environ():
    """The environ keys ``asgi.py`` publishes for a request off the bridge.

    ``lumen.queue_wait`` is deliberately absent: the real before_request hook
    derives it from the arrival mark, so leaving it out exercises that wiring.
    """
    import time
    from datetime import datetime, timezone

    from lumen.services.wsgi_disconnect import SendBlocked
    return {
        "lumen.t0_monotonic": time.monotonic() - _QUEUE_WAIT,
        "lumen.started_at": datetime.now(timezone.utc),
        "lumen.send_blocked": SendBlocked(),
    }


def _only_log(app):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        return db.session.execute(select(RequestLog)).scalar_one()


def test_non_streaming_request_records_timing_columns(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """The non-streaming /v1 path bills in the view, where the environ is live."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _capturing_openai(monkeypatch, routes, lambda **kw: _NonStreamResponse())

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": False},
        environ_base=_bridge_environ(),
    )
    assert resp.status_code == HTTPStatus.OK

    log = _only_log(app)
    assert log.started_at is not None
    assert _QUEUE_WAIT <= log.queue_wait < _MAX_PLAUSIBLE_SPAN
    assert 0 <= log.preflight < _MAX_PLAUSIBLE_SPAN
    # Nothing streams, so the first chunk is the whole response.
    assert log.ttft == log.ttft_visible == log.duration
    assert log.send_blocked == 0.0  # a holder is present; the view never blocks
    assert log.outcome == "ok"


def test_streaming_request_records_timing_columns(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """The streaming path bills inside a context-free generator.

    send_blocked is mutated *after* the first event is out, which is what tells
    a live holder read at billing time apart from a float captured in the view:
    at view time nothing has been sent, so a captured float would be 0.0 for
    ever.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _fake_openai(monkeypatch, routes, [_ContentChunk(), _UsageChunk()])

    environ = _bridge_environ()
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
        environ_base=environ,
    )
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
    assert 0 < log.ttft <= log.ttft_visible < _MAX_PLAUSIBLE_SPAN
    assert log.send_blocked == pytest.approx(_SEND_BLOCKED)
    assert log.outcome == "ok"
    assert log.aborted is False


def test_request_without_the_bridge_records_nulls_not_zeros(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """No bridge, no marks: NULL means "not measured" and must not read as zero.

    This is every request under the Werkzeug dev server and the test client.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _capturing_openai(monkeypatch, routes, lambda **kw: _NonStreamResponse())

    assert _chat_post(client, token, test_model["model_name"], False).status_code == HTTPStatus.OK

    log = _only_log(app)
    assert log.started_at is None
    assert log.queue_wait is None
    assert log.preflight is None
    assert log.send_blocked is None
    assert log.ttft is not None  # measured without the bridge's help
    assert log.outcome == "ok"


def test_aborted_stream_records_disconnect_outcome(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """A client walking away mid-stream is billed through the abort call site.

    That call site is a different function from the streaming one; a row written
    there with NULL timings loses exactly the requests this instrumentation
    exists to explain.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _fake_openai(monkeypatch, routes, [_ContentChunk(), _ContentChunk(), _UsageChunk()])

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
        environ_base=_bridge_environ(),
    )
    assert resp.is_streamed
    next(iter(resp.response))  # one event, then walk away mid-stream
    resp.close()

    log = _only_log(app)
    assert log.outcome == "disconnect"
    assert log.aborted is True
    assert log.started_at is not None
    assert _QUEUE_WAIT <= log.queue_wait < _MAX_PLAUSIBLE_SPAN
    assert 0 <= log.preflight < _MAX_PLAUSIBLE_SPAN
    assert 0 < log.ttft < _MAX_PLAUSIBLE_SPAN
