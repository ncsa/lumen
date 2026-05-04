"""Tests for the /v1 API key authentication decorator (api_key_required)."""
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
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["type"] == "invalid_request_error"


def test_non_bearer_scheme_400(client, test_model):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 400


def test_empty_bearer_token_400(client, test_model):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def test_unknown_token_401(client, test_model):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"]["type"] == "authentication_error"


def test_inactive_api_key_401(client, test_model, inactive_api_key):
    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": f"Bearer {inactive_api_key}"},
    )
    assert resp.status_code == 401


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
    assert resp.status_code == 403


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
    assert resp.status_code == 200
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
            access_type="blacklist",
        ))
        db.session.commit()

    resp = client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
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
            access_type="blacklist",
        ))
        db.session.commit()

    resp = client.get(
        f"/v1/models/{test_model['model_name']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_get_model_unknown_returns_404(client, api_key):
    token, _ = api_key
    resp = client.get(
        "/v1/models/does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Monitor token
# ---------------------------------------------------------------------------

def test_monitor_token_can_list_models(app, client, test_model):
    monitor = "monitor-secret-token"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "monitoring": {"token": monitor}}
    try:
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {monitor}"})
        assert resp.status_code == 200
        # Monitor sees all models, not filtered by entity access
        ids = [m["id"] for m in resp.get_json()["data"]]
        assert test_model["model_name"] in ids
    finally:
        app.config["YAML_DATA"].pop("monitoring", None)


def test_monitor_token_can_get_model(app, client, test_model):
    monitor = "monitor-secret-token-2"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "monitoring": {"token": monitor}}
    try:
        resp = client.get(
            f"/v1/models/{test_model['model_name']}",
            headers={"Authorization": f"Bearer {monitor}"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["id"] == test_model["model_name"]
    finally:
        app.config["YAML_DATA"].pop("monitoring", None)


def test_monitor_token_blocked_from_chat_completions(app, client):
    monitor = "monitor-secret-token-3"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                                "monitoring": {"token": monitor}}
    try:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {monitor}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"]["type"] == "authentication_error"
    finally:
        app.config["YAML_DATA"].pop("monitoring", None)


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
    assert resp.status_code == 400


def test_chat_completions_missing_model_400(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400


def test_chat_completions_unknown_model_404(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404


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
    assert resp.status_code == 503
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
            access_type="blacklist",
        ))
        db.session.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403


def test_chat_completions_graylist_no_consent_403(
    app, client, test_user, test_model, api_key,
):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="graylist",
        ))
        db.session.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403


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
        _grant_unlimited_pool(app, test_user["id"])
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="graylist",
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
    assert resp.status_code != 403


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
            access_type="whitelist",
        ))
        db.session.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"],
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code != 403
