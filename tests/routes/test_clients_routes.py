"""Tests for the clients blueprint (/clients/*)."""
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def service_client(app):
    """An active service (client) entity."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = Entity(entity_type="service", name="test-svc", initials="TS", active=True)
        db.session.add(c)
        db.session.commit()
        db.session.refresh(c)
        return {"id": c.id, "name": c.name}


@pytest.fixture
def managed_client(app, service_client, test_user):
    """service_client with test_user as manager."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(
            user_entity_id=test_user["id"],
            service_entity_id=service_client["id"],
        ))
        db.session.commit()
    return service_client


@pytest.fixture
def managed_auth_client(auth_client, managed_client):
    """auth_client fixture with test_user already managing managed_client."""
    return auth_client


# ---------------------------------------------------------------------------
# List page access
# ---------------------------------------------------------------------------

def test_clients_list_requires_login(client):
    resp = client.get("/clients", follow_redirects=False)
    assert resp.status_code == 302


def test_clients_list_empty_for_non_manager(auth_client):
    resp = auth_client.get("/clients")
    assert resp.status_code == 200


def test_clients_list_shows_managed_client(managed_auth_client, managed_client):
    resp = managed_auth_client.get("/clients")
    assert resp.status_code == 200
    assert managed_client["name"].encode() in resp.data


def test_clients_list_admin_sees_all(app, admin_client, service_client):
    resp = admin_client.get("/clients")
    assert resp.status_code == 200
    assert service_client["name"].encode() in resp.data


# ---------------------------------------------------------------------------
# Detail page access
# ---------------------------------------------------------------------------

def test_detail_requires_login(client, service_client):
    resp = client.get(f"/clients/{service_client['id']}", follow_redirects=False)
    assert resp.status_code == 302


def test_detail_forbidden_for_non_manager(auth_client, service_client):
    resp = auth_client.get(f"/clients/{service_client['id']}")
    assert resp.status_code == 403


def test_detail_loads_for_manager(managed_auth_client, managed_client):
    resp = managed_auth_client.get(f"/clients/{managed_client['id']}")
    assert resp.status_code == 200
    assert managed_client["name"].encode() in resp.data


def test_detail_loads_for_admin(admin_client, service_client):
    resp = admin_client.get(f"/clients/{service_client['id']}")
    assert resp.status_code == 200


def test_detail_404_for_unknown(admin_client):
    resp = admin_client.get("/clients/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Create client
# ---------------------------------------------------------------------------

def test_create_client_requires_admin(auth_client):
    resp = auth_client.post("/clients", json={"name": "new-svc"})
    assert resp.status_code == 403


def test_create_client_succeeds(app, admin_client):
    resp = admin_client.post("/clients", json={"name": "created-svc"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "created-svc"
    with app.app_context():
        from lumen.models.entity import Entity
        c = Entity.query.filter_by(name="created-svc", entity_type="service").first()
        assert c is not None
        assert c.active is True


def test_create_client_empty_name_returns_400(admin_client):
    resp = admin_client.post("/clients", json={"name": "  "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Toggle client
# ---------------------------------------------------------------------------

def test_toggle_requires_admin(managed_auth_client, managed_client):
    resp = managed_auth_client.post(f"/clients/{managed_client['id']}/toggle")
    assert resp.status_code == 403


def test_toggle_deactivates_active_client(app, admin_client, service_client):
    resp = admin_client.post(f"/clients/{service_client['id']}/toggle")
    assert resp.status_code == 200
    assert resp.get_json()["active"] is False
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.get(Entity, service_client["id"])
        assert c.active is False


def test_toggle_reactivates_inactive_client(app, admin_client, service_client):
    admin_client.post(f"/clients/{service_client['id']}/toggle")  # deactivate
    resp = admin_client.post(f"/clients/{service_client['id']}/toggle")  # reactivate
    assert resp.get_json()["active"] is True


# ---------------------------------------------------------------------------
# Delete (soft) client
# ---------------------------------------------------------------------------

def test_delete_client_requires_admin(managed_auth_client, managed_client):
    resp = managed_auth_client.delete(f"/clients/{managed_client['id']}")
    assert resp.status_code == 403


def test_delete_client_soft_deletes(app, admin_client, service_client):
    resp = admin_client.delete(f"/clients/{service_client['id']}")
    assert resp.status_code == 204
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.get(Entity, service_client["id"])
        assert c.active is False


# ---------------------------------------------------------------------------
# Manager management
# ---------------------------------------------------------------------------

def test_add_manager_requires_admin(managed_auth_client, managed_client):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/users",
        json={"email": "anyone@example.com"},
    )
    assert resp.status_code == 403


def test_add_manager_succeeds(app, admin_client, service_client, test_user):
    resp = admin_client.post(
        f"/clients/{service_client['id']}/users",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["email"] == "testuser@example.com"
    with app.app_context():
        from lumen.models.entity_manager import EntityManager
        assoc = EntityManager.query.filter_by(
            user_entity_id=test_user["id"],
            service_entity_id=service_client["id"],
        ).first()
        assert assoc is not None


def test_add_manager_unknown_email_returns_404(admin_client, service_client):
    resp = admin_client.post(
        f"/clients/{service_client['id']}/users",
        json={"email": "nobody@example.com"},
    )
    assert resp.status_code == 404


def test_add_manager_duplicate_returns_409(admin_client, managed_client, test_user):
    resp = admin_client.post(
        f"/clients/{managed_client['id']}/users",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == 409


def test_add_manager_missing_email_returns_400(admin_client, service_client):
    resp = admin_client.post(f"/clients/{service_client['id']}/users", json={})
    assert resp.status_code == 400


def test_remove_manager_requires_admin(managed_auth_client, managed_client, test_user):
    resp = managed_auth_client.delete(
        f"/clients/{managed_client['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == 403


def test_remove_manager_succeeds(app, admin_client, managed_client, test_user):
    resp = admin_client.delete(
        f"/clients/{managed_client['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == 204
    with app.app_context():
        from lumen.models.entity_manager import EntityManager
        assoc = EntityManager.query.filter_by(
            user_entity_id=test_user["id"],
            service_entity_id=managed_client["id"],
        ).first()
        assert assoc is None


def test_remove_manager_not_found_returns_404(admin_client, service_client, test_user):
    resp = admin_client.delete(
        f"/clients/{service_client['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def test_create_key_forbidden_for_non_manager(auth_client, service_client):
    resp = auth_client.post(
        f"/clients/{service_client['id']}/keys",
        json={"name": "prod", "key": "sk_test123"},
    )
    assert resp.status_code == 403


def test_create_key_invalid_prefix_returns_400(managed_auth_client, managed_client):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "prod", "key": "bad-key-no-prefix"},
    )
    assert resp.status_code == 400


def test_create_key_succeeds(app, managed_auth_client, managed_client):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "prod", "key": "sk_testkey12345678"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "prod"
    with app.app_context():
        from lumen.models.api_key import APIKey
        key = APIKey.query.filter_by(entity_id=managed_client["id"], name="prod").first()
        assert key is not None
        assert key.active is True


def test_create_key_duplicate_returns_409(managed_auth_client, managed_client):
    managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "key1", "key": "sk_dupekey123456789"},
    )
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "key2", "key": "sk_dupekey123456789"},
    )
    assert resp.status_code == 409


def test_create_key_admin_succeeds(app, admin_client, service_client):
    resp = admin_client.post(
        f"/clients/{service_client['id']}/keys",
        json={"name": "admin-key", "key": "sk_adminkey123456"},
    )
    assert resp.status_code == 201


def test_delete_key_forbidden_for_non_manager(app, auth_client, service_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        key = APIKey(
            entity_id=service_client["id"], name="k",
            key_hash=hash_api_key("sk_delkey1234567890"),
            key_hint="sk_delke...7890", active=True,
        )
        db.session.add(key)
        db.session.commit()
        key_id = key.id

    resp = auth_client.delete(f"/clients/{service_client['id']}/keys/{key_id}")
    assert resp.status_code == 403


def test_delete_key_soft_deletes(app, managed_auth_client, managed_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        key = APIKey(
            entity_id=managed_client["id"], name="to-delete",
            key_hash=hash_api_key("sk_todelete12345678"),
            key_hint="sk_todel...5678", active=True,
        )
        db.session.add(key)
        db.session.commit()
        key_id = key.id

    resp = managed_auth_client.delete(f"/clients/{managed_client['id']}/keys/{key_id}")
    assert resp.status_code == 204
    with app.app_context():
        from lumen.models.api_key import APIKey
        from lumen.extensions import db
        k = db.session.get(APIKey, key_id)
        assert k.active is False


# ---------------------------------------------------------------------------
# Graylist consent
# ---------------------------------------------------------------------------

def test_consent_forbidden_for_non_manager(app, auth_client, service_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(
            model_config_id=test_model["id"], access_type="graylist"
        ))
        db.session.commit()

    resp = auth_client.post(
        f"/clients/{service_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == 403


def test_consent_non_graylist_model_returns_400(app, managed_auth_client, managed_client, test_model):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == 400


def test_consent_graylist_model_succeeds(app, managed_auth_client, managed_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(
            model_config_id=test_model["id"], access_type="graylist"
        ))
        db.session.commit()

    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    with app.app_context():
        from lumen.models.entity_model_consent import EntityModelConsent
        consent = EntityModelConsent.query.filter_by(
            entity_id=managed_client["id"],
            model_config_id=test_model["id"],
        ).first()
        assert consent is not None


def test_consent_idempotent(app, managed_auth_client, managed_client, test_model):
    """Consenting twice doesn't create duplicate rows."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(
            model_config_id=test_model["id"], access_type="graylist"
        ))
        db.session.commit()

    managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == 200
    with app.app_context():
        from lumen.models.entity_model_consent import EntityModelConsent
        count = EntityModelConsent.query.filter_by(
            entity_id=managed_client["id"],
            model_config_id=test_model["id"],
        ).count()
        assert count == 1


# ---------------------------------------------------------------------------
# Client API key end-to-end: create via route then authenticate against /v1/
# ---------------------------------------------------------------------------

def _grant_unlimited_pool(app, entity_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()


def test_client_key_created_via_route_can_authenticate(
    app, client, managed_auth_client, managed_client, test_model, test_model_endpoint
):
    """Key created through POST /clients/<sid>/keys works for /v1/ auth."""
    _grant_unlimited_pool(app, managed_client["id"])

    # Create key via the clients route
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "e2e-key", "key": "sk_e2etest1234567890"},
    )
    assert resp.status_code == 201
    token = resp.get_json()["key"]

    # Use the key to list models
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.get_json()["object"] == "list"


def test_client_key_lists_accessible_model(
    app, client, managed_auth_client, managed_client, test_model, test_model_endpoint
):
    """Client key sees models it has access to."""
    _grant_unlimited_pool(app, managed_client["id"])

    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "model-key", "key": "sk_modelkey12345678"},
    )
    token = resp.get_json()["key"]

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.get_json()["data"]]
    assert test_model["model_name"] in ids


def test_client_key_blocked_after_soft_delete(
    app, client, managed_auth_client, managed_client, test_model_endpoint
):
    """Key deactivated via DELETE /clients/<sid>/keys/<kid> returns 401."""
    _grant_unlimited_pool(app, managed_client["id"])

    # Create key
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "del-key", "key": "sk_deletekey12345678"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    token, kid = data["key"], data["id"]

    # Confirm it works
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    # Soft-delete the key
    resp = managed_auth_client.delete(f"/clients/{managed_client['id']}/keys/{kid}")
    assert resp.status_code == 204

    # Now it should be rejected
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_client_key_no_pool_returns_403(
    app, client, managed_auth_client, managed_client, test_model, test_model_endpoint
):
    """Client key with no coin pool is denied on chat completions (no EntityLimit → 403)."""
    # No pool granted — service entity has no EntityLimit

    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "nopool-key", "key": "sk_nopoolkey12345678"},
    )
    token = resp.get_json()["key"]

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403
