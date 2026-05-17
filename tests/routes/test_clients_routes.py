"""Tests for the clients blueprint (/clients/*)."""
from http import HTTPStatus
import pytest
from sqlalchemy import func, select


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def service_client(app):
    """An active service (client) entity."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = Entity(entity_type="client", name="test-svc", initials="TS", active=True)
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
            client_entity_id=service_client["id"],
        ))
        db.session.commit()
    return service_client


@pytest.fixture
def managed_auth_client(auth_client, managed_client):
    """auth_client fixture with test_user already managing managed_client."""
    return auth_client


@pytest.fixture
def make_api_key(app):
    """Factory: create an APIKey row for any entity_id. Returns (key_id, raw_key)."""
    def _make(entity_id, raw_key="sk_testkey12345678", name="test-key"):
        with app.app_context():
            from lumen.extensions import db
            from lumen.models.api_key import APIKey
            from lumen.services.crypto import hash_api_key
            key = APIKey(
                entity_id=entity_id,
                name=name,
                key_hash=hash_api_key(raw_key),
                key_hint=f"{raw_key[:8]}...{raw_key[-4:]}",
                active=True,
            )
            db.session.add(key)
            db.session.commit()
            return key.id, raw_key
    return _make


@pytest.fixture
def make_graylist_access(app):
    """Factory: grant graylist EntityModelAccess for any (entity_id, model_config_id)."""
    def _make(entity_id, model_config_id):
        with app.app_context():
            from lumen.extensions import db
            from lumen.models.entity_model_access import EntityModelAccess
            db.session.add(EntityModelAccess(
                entity_id=entity_id,
                model_config_id=model_config_id,
                access_type="graylist",
            ))
            db.session.commit()
    return _make


@pytest.fixture
def unlimited_pool(app, managed_client):
    """Grant managed_client an unlimited coin pool."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=managed_client["id"], max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()


# ---------------------------------------------------------------------------
# List page access
# ---------------------------------------------------------------------------

def test_clients_list_requires_login(client):
    resp = client.get("/clients", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_clients_list_empty_for_non_manager(auth_client):
    resp = auth_client.get("/clients")
    assert resp.status_code == HTTPStatus.OK


def test_clients_list_shows_managed_client(managed_auth_client, managed_client):
    resp = managed_auth_client.get("/clients")
    assert resp.status_code == HTTPStatus.OK
    assert managed_client["name"].encode() in resp.data


def test_clients_list_admin_sees_all(app, admin_client, service_client):
    resp = admin_client.get("/clients")
    assert resp.status_code == HTTPStatus.OK
    assert service_client["name"].encode() in resp.data


def test_clients_list_shows_entity_stats(app, admin_client, service_client, test_model):
    """Client listing reads usage from entity_stats, not a live GROUP BY."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        db.session.add(EntityStat(
            entity_id=service_client["id"],
            requests=42, input_tokens=1000, output_tokens=500, cost="0.05",
        ))
        db.session.commit()

    resp = admin_client.get("/clients")
    assert resp.status_code == HTTPStatus.OK
    # The page should render without error; spot-check the values appear
    assert b"42" in resp.data


def test_clients_list_zero_stats_without_entity_stat(admin_client, service_client):
    """Clients with no entity_stats row show zero usage, not an error."""
    resp = admin_client.get("/clients")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Detail page access
# ---------------------------------------------------------------------------

def test_detail_requires_login(client, service_client):
    resp = client.get(f"/clients/{service_client['id']}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_detail_forbidden_for_non_manager(auth_client, service_client):
    resp = auth_client.get(f"/clients/{service_client['id']}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_detail_loads_for_manager(managed_auth_client, managed_client):
    resp = managed_auth_client.get(f"/clients/{managed_client['id']}")
    assert resp.status_code == HTTPStatus.OK
    assert managed_client["name"].encode() in resp.data


def test_detail_loads_for_admin(admin_client, service_client):
    resp = admin_client.get(f"/clients/{service_client['id']}")
    assert resp.status_code == HTTPStatus.OK


def test_detail_404_for_unknown(admin_client):
    resp = admin_client.get("/clients/99999")
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Create client
# ---------------------------------------------------------------------------

def test_create_client_requires_admin(auth_client):
    resp = auth_client.post("/clients", json={"name": "new-svc"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_create_client_succeeds(app, admin_client):
    resp = admin_client.post("/clients", json={"name": "created-svc"})
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["name"] == "created-svc"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.execute(select(Entity).filter_by(name="created-svc", entity_type="client")).scalar_one_or_none()
        assert c is not None
        assert c.active is True


def test_create_client_empty_name_returns_400(admin_client):
    resp = admin_client.post("/clients", json={"name": "  "})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Toggle client
# ---------------------------------------------------------------------------

def test_toggle_requires_admin(managed_auth_client, managed_client):
    resp = managed_auth_client.post(f"/clients/{managed_client['id']}/toggle")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_toggle_deactivates_active_client(app, admin_client, service_client):
    resp = admin_client.post(f"/clients/{service_client['id']}/toggle")
    assert resp.status_code == HTTPStatus.OK
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
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_delete_client_soft_deletes(app, admin_client, service_client):
    resp = admin_client.delete(f"/clients/{service_client['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
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
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_add_manager_succeeds(app, admin_client, service_client, test_user):
    resp = admin_client.post(
        f"/clients/{service_client['id']}/users",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["email"] == "testuser@example.com"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], client_entity_id=service_client["id"])
        ).scalar_one_or_none()
        assert assoc is not None


def test_add_manager_unknown_email_returns_404(admin_client, service_client):
    resp = admin_client.post(
        f"/clients/{service_client['id']}/users",
        json={"email": "nobody@example.com"},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_add_manager_duplicate_returns_409(admin_client, managed_client, test_user):
    resp = admin_client.post(
        f"/clients/{managed_client['id']}/users",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == HTTPStatus.CONFLICT


def test_add_manager_missing_email_returns_400(admin_client, service_client):
    resp = admin_client.post(f"/clients/{service_client['id']}/users", json={})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_remove_manager_requires_admin(managed_auth_client, managed_client, test_user):
    resp = managed_auth_client.delete(
        f"/clients/{managed_client['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_remove_manager_succeeds(app, admin_client, managed_client, test_user):
    resp = admin_client.delete(
        f"/clients/{managed_client['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], client_entity_id=managed_client["id"])
        ).scalar_one_or_none()
        assert assoc is None


def test_remove_manager_not_found_returns_404(admin_client, service_client, test_user):
    resp = admin_client.delete(
        f"/clients/{service_client['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def test_create_key_forbidden_for_non_manager(auth_client, service_client):
    resp = auth_client.post(
        f"/clients/{service_client['id']}/keys",
        json={"name": "prod", "key": "sk_test123"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_create_key_invalid_prefix_returns_400(managed_auth_client, managed_client):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "prod", "key": "bad-key-no-prefix"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_key_succeeds(app, managed_auth_client, managed_client):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "prod", "key": "sk_testkey12345678"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["name"] == "prod"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        key = db.session.execute(select(APIKey).filter_by(entity_id=managed_client["id"], name="prod")).scalar_one_or_none()
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
    assert resp.status_code == HTTPStatus.CONFLICT


def test_create_key_admin_succeeds(app, admin_client, service_client):
    resp = admin_client.post(
        f"/clients/{service_client['id']}/keys",
        json={"name": "admin-key", "key": "sk_adminkey123456"},
    )
    assert resp.status_code == HTTPStatus.CREATED


def test_delete_key_forbidden_for_non_manager(auth_client, service_client, make_api_key):
    key_id, _ = make_api_key(service_client["id"], raw_key="sk_delkey1234567890", name="k")
    resp = auth_client.delete(f"/clients/{service_client['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_delete_key_soft_deletes(app, managed_auth_client, managed_client, make_api_key):
    key_id, _ = make_api_key(managed_client["id"], raw_key="sk_todelete12345678", name="to-delete")
    resp = managed_auth_client.delete(f"/clients/{managed_client['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        k = db.session.get(APIKey, key_id)
        assert k is None


# ---------------------------------------------------------------------------
# Graylist consent
# ---------------------------------------------------------------------------

def test_consent_forbidden_for_non_manager(auth_client, service_client, test_model, make_graylist_access):
    make_graylist_access(service_client["id"], test_model["id"])
    resp = auth_client.post(
        f"/clients/{service_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_consent_non_graylist_model_returns_400(app, managed_auth_client, managed_client, test_model):
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_consent_graylist_model_succeeds(app, managed_auth_client, managed_client, test_model, make_graylist_access):
    make_graylist_access(managed_client["id"], test_model["id"])
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["ok"] is True
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        consent = db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=managed_client["id"], model_config_id=test_model["id"])
        ).scalar_one_or_none()
        assert consent is not None


def test_consent_idempotent(app, managed_auth_client, managed_client, test_model, make_graylist_access):
    """Consenting twice doesn't create duplicate rows."""
    make_graylist_access(managed_client["id"], test_model["id"])
    managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        count = db.session.scalar(
            select(func.count()).select_from(EntityModelConsent).filter_by(
                entity_id=managed_client["id"], model_config_id=test_model["id"]
            )
        )
        assert count == 1


# ---------------------------------------------------------------------------
# Client API key end-to-end: create via route then authenticate against /v1/
# ---------------------------------------------------------------------------

def test_client_key_created_via_route_can_authenticate(
    client, managed_auth_client, managed_client, test_model, test_model_endpoint, unlimited_pool
):
    """Key created through POST /clients/<sid>/keys works for /v1/ auth."""
    # Create key via the clients route
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "e2e-key", "key": "sk_e2etest1234567890"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    token = resp.get_json()["key"]

    # Use the key to list models
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["object"] == "list"


def test_client_key_lists_accessible_model(
    client, managed_auth_client, managed_client, test_model, test_model_endpoint, unlimited_pool
):
    """Client key sees models it has access to."""
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "model-key", "key": "sk_modelkey12345678"},
    )
    token = resp.get_json()["key"]

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    ids = [m["id"] for m in resp.get_json()["data"]]
    assert test_model["model_name"] in ids


def test_client_key_blocked_after_soft_delete(
    client, managed_auth_client, managed_client, test_model_endpoint, unlimited_pool
):
    """Key deactivated via DELETE /clients/<sid>/keys/<kid> returns 401."""
    # Create key
    resp = managed_auth_client.post(
        f"/clients/{managed_client['id']}/keys",
        json={"name": "del-key", "key": "sk_deletekey12345678"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    token, kid = data["key"], data["id"]

    # Confirm it works
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK

    # Soft-delete the key
    resp = managed_auth_client.delete(f"/clients/{managed_client['id']}/keys/{kid}")
    assert resp.status_code == HTTPStatus.NO_CONTENT

    # Now it should be rejected
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


def test_client_key_no_pool_returns_403(
    client, managed_auth_client, managed_client, test_model, test_model_endpoint
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
    assert resp.status_code == HTTPStatus.FORBIDDEN
