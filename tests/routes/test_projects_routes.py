"""Tests for the projects blueprint (/projects/*)."""
from http import HTTPStatus
import pytest
from sqlalchemy import func, select


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def service_project(app):
    """An active service (project) entity."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = Entity(entity_type="project", name="test-svc", initials="TS", active=True)
        db.session.add(c)
        db.session.commit()
        db.session.refresh(c)
        return {"id": c.id, "name": c.name}


@pytest.fixture
def managed_project(app, service_project, test_user):
    """service_project with test_user as manager (non-owner)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(
            user_entity_id=test_user["id"],
            project_entity_id=service_project["id"],
        ))
        db.session.commit()
    return service_project


@pytest.fixture
def owned_project(app, service_project, test_user):
    """service_project with test_user as owner (is_owner=True)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(
            user_entity_id=test_user["id"],
            project_entity_id=service_project["id"],
            is_owner=True,
        ))
        db.session.commit()
    return service_project


@pytest.fixture
def owner_auth_client(auth_client, owned_project):
    """auth_client with test_user as owner of owned_project."""
    return auth_client


@pytest.fixture
def second_user(app):
    """A second user for transfer/add-manager tests."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = Entity(
            entity_type="user",
            email="second@example.com",
            name="Second User",
            initials="SU",
            gravatar_hash="ghi789",
            active=True,
        )
        db.session.add(entity)
        db.session.commit()
        db.session.refresh(entity)
        return {"id": entity.id, "name": entity.name, "email": entity.email}


@pytest.fixture
def managed_auth_client(auth_client, managed_project):
    """auth_client fixture with test_user already managing managed_project."""
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
    """Factory: mark the model needs_ack and grant allowed access (resolves to needs_ack)."""
    def _make(entity_id, model_config_id):
        with app.app_context():
            from lumen.extensions import db
            from lumen.models.entity_model_access import EntityModelAccess
            from lumen.models.model_config import ModelConfig
            db.session.get(ModelConfig, model_config_id).needs_ack = True
            db.session.add(EntityModelAccess(
                entity_id=entity_id,
                model_config_id=model_config_id,
                access_type="allowed",
            ))
            db.session.commit()
    return _make


@pytest.fixture
def writable_config(app, tmp_path):
    """Point CONFIG_YAML at a writable temp copy so create_project's write-back
    doesn't mutate the shared committed fixture. Restores afterwards."""
    import shutil
    cfg = tmp_path / "config.yaml"
    shutil.copy(app.config["CONFIG_YAML"], cfg)
    original_path = app.config["CONFIG_YAML"]
    original_data = app.config.get("YAML_DATA")
    app.config["CONFIG_YAML"] = str(cfg)
    yield cfg
    app.config["CONFIG_YAML"] = original_path
    app.config["YAML_DATA"] = original_data


@pytest.fixture
def unlimited_pool(app, managed_project):
    """Grant managed_project an unlimited coin pool."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=managed_project["id"], max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()


# ---------------------------------------------------------------------------
# List page access
# ---------------------------------------------------------------------------

def test_projects_list_requires_login(client):
    resp = client.get("/projects", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_projects_list_empty_for_non_manager(auth_client):
    resp = auth_client.get("/projects")
    assert resp.status_code == HTTPStatus.OK


def test_projects_list_shows_managed_project(managed_auth_client, managed_project):
    resp = managed_auth_client.get("/projects")
    assert resp.status_code == HTTPStatus.OK
    # Rows are loaded via the /projects/data API, not embedded in the page.
    data = managed_auth_client.get("/projects/data").get_json()
    assert managed_project["name"] in [c["name"] for c in data["projects"]]


def test_projects_list_admin_sees_all(app, admin_client, service_project):
    resp = admin_client.get("/projects")
    assert resp.status_code == HTTPStatus.OK
    data = admin_client.get("/projects/data").get_json()
    assert service_project["name"] in [c["name"] for c in data["projects"]]


def test_projects_list_shows_entity_stats(app, admin_client, service_project, test_model):
    """Project listing reads usage from entity_stats, not a live GROUP BY."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        db.session.add(EntityStat(
            entity_id=service_project["id"],
            requests=42, input_tokens=1000, output_tokens=500, cost="0.05",
        ))
        db.session.commit()

    resp = admin_client.get("/projects")
    assert resp.status_code == HTTPStatus.OK
    # The page should render without error; spot-check the values appear
    assert b"42" in resp.data


def test_projects_list_zero_stats_without_entity_stat(admin_client, service_project):
    """Projects with no entity_stats row show zero usage, not an error."""
    resp = admin_client.get("/projects")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Detail page access
# ---------------------------------------------------------------------------

def test_detail_requires_login(client, service_project):
    resp = client.get(f"/projects/{service_project['id']}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_detail_forbidden_for_non_manager(auth_client, service_project):
    resp = auth_client.get(f"/projects/{service_project['id']}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_detail_loads_for_manager(managed_auth_client, managed_project):
    resp = managed_auth_client.get(f"/projects/{managed_project['id']}")
    assert resp.status_code == HTTPStatus.OK
    assert managed_project["name"].encode() in resp.data


def test_detail_loads_for_admin(admin_client, service_project):
    resp = admin_client.get(f"/projects/{service_project['id']}")
    assert resp.status_code == HTTPStatus.OK


def test_detail_404_for_unknown(admin_client):
    resp = admin_client.get("/projects/99999")
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Create project
# ---------------------------------------------------------------------------

def test_create_project_requires_admin(auth_client):
    resp = auth_client.post("/projects", json={"name": "new-svc"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_create_project_succeeds(app, admin_client, writable_config):
    resp = admin_client.post("/projects", json={"name": "created-svc"})
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["name"] == "created-svc"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.execute(select(Entity).filter_by(name="created-svc", entity_type="project")).scalar_one_or_none()
        assert c is not None
        assert c.active is True


def test_create_project_writes_empty_config_entry(admin_client, writable_config):
    """Creating a project records an empty entry in config.yaml so the file reflects it."""
    import yaml
    resp = admin_client.post("/projects", json={"name": "cfg-svc"})
    assert resp.status_code == HTTPStatus.CREATED
    saved = yaml.safe_load(writable_config.read_text()) or {}
    assert "cfg-svc" in saved.get("projects", {})
    assert saved["projects"]["cfg-svc"] == {}


def test_create_project_empty_name_returns_400(admin_client):
    resp = admin_client.post("/projects", json={"name": "  "})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Toggle project
# ---------------------------------------------------------------------------

def test_toggle_requires_admin(managed_auth_client, managed_project):
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/toggle")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_toggle_deactivates_active_project(app, admin_client, service_project):
    resp = admin_client.post(f"/projects/{service_project['id']}/toggle")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["active"] is False
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.get(Entity, service_project["id"])
        assert c.active is False


def test_toggle_reactivates_inactive_project(app, admin_client, service_project):
    admin_client.post(f"/projects/{service_project['id']}/toggle")  # deactivate
    resp = admin_client.post(f"/projects/{service_project['id']}/toggle")  # reactivate
    assert resp.get_json()["active"] is True


# ---------------------------------------------------------------------------
# Project data API (pagination + show disabled)
# ---------------------------------------------------------------------------

def test_projects_data_requires_login(client):
    resp = client.get("/projects/data", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_projects_data_lists_active_project(admin_client, service_project):
    resp = admin_client.get("/projects/data")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["page"] == 1
    assert data["per_page"] == 25
    names = [c["name"] for c in data["projects"]]
    assert service_project["name"] in names


def test_projects_data_hides_disabled_by_default(admin_client, service_project):
    admin_client.post(f"/projects/{service_project['id']}/toggle")  # deactivate
    resp = admin_client.get("/projects/data")
    names = [c["name"] for c in resp.get_json()["projects"]]
    assert service_project["name"] not in names


def test_projects_data_show_disabled_reveals(admin_client, service_project):
    admin_client.post(f"/projects/{service_project['id']}/toggle")  # deactivate
    resp = admin_client.get("/projects/data?show_disabled=1")
    names = [c["name"] for c in resp.get_json()["projects"]]
    assert service_project["name"] in names


def test_projects_data_invalid_per_page_falls_back(admin_client, service_project):
    resp = admin_client.get("/projects/data?per_page=7")
    assert resp.get_json()["per_page"] == 25


# ---------------------------------------------------------------------------
# Delete (soft) project
# ---------------------------------------------------------------------------

def test_delete_project_requires_admin(managed_auth_client, managed_project):
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_delete_project_soft_deletes(app, admin_client, service_project):
    resp = admin_client.delete(f"/projects/{service_project['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.get(Entity, service_project["id"])
        assert c.active is False


# ---------------------------------------------------------------------------
# Manager management
# ---------------------------------------------------------------------------

def test_add_manager_requires_admin(managed_auth_client, managed_project):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/users",
        json={"email": "anyone@example.com"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_add_manager_succeeds(app, admin_client, service_project, test_user):
    resp = admin_client.post(
        f"/projects/{service_project['id']}/users",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["email"] == "testuser@example.com"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=service_project["id"])
        ).scalar_one_or_none()
        assert assoc is not None


def test_add_manager_unknown_email_returns_404(admin_client, service_project):
    resp = admin_client.post(
        f"/projects/{service_project['id']}/users",
        json={"email": "nobody@example.com"},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_add_manager_duplicate_returns_409(admin_client, managed_project, test_user):
    resp = admin_client.post(
        f"/projects/{managed_project['id']}/users",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == HTTPStatus.CONFLICT


def test_add_manager_missing_email_returns_400(admin_client, service_project):
    resp = admin_client.post(f"/projects/{service_project['id']}/users", json={})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_remove_manager_requires_admin(managed_auth_client, managed_project, test_user):
    resp = managed_auth_client.delete(
        f"/projects/{managed_project['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_remove_manager_succeeds(app, admin_client, managed_project, test_user):
    resp = admin_client.delete(
        f"/projects/{managed_project['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=managed_project["id"])
        ).scalar_one_or_none()
        assert assoc is None


def test_remove_manager_not_found_returns_404(admin_client, service_project, test_user):
    resp = admin_client.delete(
        f"/projects/{service_project['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Owner / project-admin functionality
# ---------------------------------------------------------------------------

def test_create_project_with_owner(app, admin_client, writable_config, test_user):
    resp = admin_client.post("/projects", json={"name": "owned-svc", "owner_email": "testuser@example.com"})
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        project = db.session.execute(select(Entity).filter_by(name="owned-svc", entity_type="project")).scalar_one()
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=project.id)
        ).scalar_one()
        assert assoc.is_owner is True


def test_create_project_owner_not_found_returns_404(admin_client, writable_config):
    resp = admin_client.post("/projects", json={"name": "bad-owner", "owner_email": "nobody@example.com"})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_create_project_without_owner_is_ownerless(app, admin_client, writable_config):
    resp = admin_client.post("/projects", json={"name": "no-owner"})
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        project = db.session.execute(select(Entity).filter_by(name="no-owner", entity_type="project")).scalar_one()
        count = db.session.scalar(
            select(func.count()).select_from(EntityManager).filter_by(project_entity_id=project.id)
        )
        assert count == 0


def test_owner_can_add_manager(owner_auth_client, owned_project, second_user):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/users",
        json={"email": "second@example.com"},
    )
    assert resp.status_code == HTTPStatus.CREATED


def test_owner_can_remove_manager(app, owner_auth_client, owned_project, second_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(
            user_entity_id=second_user["id"],
            project_entity_id=owned_project["id"],
        ))
        db.session.commit()
    resp = owner_auth_client.delete(
        f"/projects/{owned_project['id']}/users/{second_user['id']}"
    )
    assert resp.status_code == HTTPStatus.NO_CONTENT


def test_owner_cannot_remove_self(owner_auth_client, owned_project, test_user):
    resp = owner_auth_client.delete(
        f"/projects/{owned_project['id']}/users/{test_user['id']}"
    )
    assert resp.status_code == HTTPStatus.CONFLICT


def test_owner_can_toggle(app, owner_auth_client, owned_project):
    resp = owner_auth_client.post(f"/projects/{owned_project['id']}/toggle")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["active"] is False


def test_non_owner_manager_cannot_toggle(managed_auth_client, managed_project):
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/toggle")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_non_owner_manager_cannot_add_manager(managed_auth_client, managed_project, second_user):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/users",
        json={"email": "second@example.com"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_owner_can_search_users(owner_auth_client, owned_project):
    resp = owner_auth_client.get(f"/projects/{owned_project['id']}/users/search?q=test")
    assert resp.status_code == HTTPStatus.OK


def test_transfer_ownership(app, owner_auth_client, owned_project, test_user, second_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(
            user_entity_id=second_user["id"],
            project_entity_id=owned_project["id"],
            is_owner=False,
        ))
        db.session.commit()
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        old = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=owned_project["id"])
        ).scalar_one()
        new = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=second_user["id"], project_entity_id=owned_project["id"])
        ).scalar_one()
        assert old.is_owner is False
        assert new.is_owner is True


def test_transfer_to_non_manager_creates_manager(app, owner_auth_client, owned_project, test_user, second_user):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        old = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=owned_project["id"])
        ).scalar_one()
        new = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=second_user["id"], project_entity_id=owned_project["id"])
        ).scalar_one()
        assert old.is_owner is False
        assert new.is_owner is True


def test_transfer_requires_owner_or_admin(managed_auth_client, managed_project, second_user):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_transfer_unknown_user_returns_404(owner_auth_client, owned_project):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={"user_id": 99999},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_transfer_missing_user_id_returns_400(owner_auth_client, owned_project):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_transfer_to_current_owner_returns_409(owner_auth_client, owned_project, test_user):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={"user_id": test_user["id"]},
    )
    assert resp.status_code == HTTPStatus.CONFLICT


def test_admin_assigns_owner_to_ownerless_project(app, admin_client, service_project, second_user):
    """Admin can assign an owner to a project that has no owner, even when the
    target user is not already a manager (the None-is-None edge case)."""
    resp = admin_client.post(
        f"/projects/{service_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        assoc = db.session.execute(
            select(EntityManager).filter_by(
                user_entity_id=second_user["id"], project_entity_id=service_project["id"]
            )
        ).scalar_one()
        assert assoc.is_owner is True


def test_plain_non_manager_forbidden_on_owner_routes(auth_client, service_project, second_user):
    """A user who is neither admin, owner, nor manager gets 403 on owner-level routes."""
    resp = auth_client.post(
        f"/projects/{service_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def test_create_key_forbidden_for_non_manager(auth_client, service_project):
    resp = auth_client.post(
        f"/projects/{service_project['id']}/keys",
        json={"name": "prod", "key": "sk_test123"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_create_key_invalid_prefix_returns_400(managed_auth_client, managed_project):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "prod", "key": "bad-key-no-prefix"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_key_succeeds(app, managed_auth_client, managed_project):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "prod", "key": "sk_testkey12345678"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["name"] == "prod"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        key = db.session.execute(select(APIKey).filter_by(entity_id=managed_project["id"], name="prod")).scalar_one_or_none()
        assert key is not None
        assert key.active is True


def test_create_key_duplicate_returns_409(managed_auth_client, managed_project):
    managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "key1", "key": "sk_dupekey123456789"},
    )
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "key2", "key": "sk_dupekey123456789"},
    )
    assert resp.status_code == HTTPStatus.CONFLICT


def test_create_key_admin_succeeds(app, admin_client, service_project):
    resp = admin_client.post(
        f"/projects/{service_project['id']}/keys",
        json={"name": "admin-key", "key": "sk_adminkey123456"},
    )
    assert resp.status_code == HTTPStatus.CREATED


def test_delete_key_forbidden_for_non_manager(auth_client, service_project, make_api_key):
    key_id, _ = make_api_key(service_project["id"], raw_key="sk_delkey1234567890", name="k")
    resp = auth_client.delete(f"/projects/{service_project['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_delete_key_soft_deletes(app, managed_auth_client, managed_project, make_api_key):
    key_id, _ = make_api_key(managed_project["id"], raw_key="sk_todelete12345678", name="to-delete")
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        k = db.session.get(APIKey, key_id)
        assert k is None


# ---------------------------------------------------------------------------
# Graylist consent
# ---------------------------------------------------------------------------

def test_consent_forbidden_for_non_manager(auth_client, service_project, test_model, make_graylist_access):
    make_graylist_access(service_project["id"], test_model["id"])
    resp = auth_client.post(
        f"/projects/{service_project['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_consent_non_graylist_model_returns_400(app, managed_auth_client, managed_project, test_model):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_consent_graylist_model_succeeds(app, managed_auth_client, managed_project, test_model, make_graylist_access):
    make_graylist_access(managed_project["id"], test_model["id"])
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["ok"] is True
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        consent = db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=managed_project["id"], model_config_id=test_model["id"])
        ).scalar_one_or_none()
        assert consent is not None


def test_consent_idempotent(app, managed_auth_client, managed_project, test_model, make_graylist_access):
    """Consenting twice doesn't create duplicate rows."""
    make_graylist_access(managed_project["id"], test_model["id"])
    managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/{test_model['model_name']}"
    )
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        count = db.session.scalar(
            select(func.count()).select_from(EntityModelConsent).filter_by(
                entity_id=managed_project["id"], model_config_id=test_model["id"]
            )
        )
        assert count == 1


# ---------------------------------------------------------------------------
# Project API key end-to-end: create via route then authenticate against /v1/
# ---------------------------------------------------------------------------

def test_project_key_created_via_route_can_authenticate(
    client, managed_auth_client, managed_project, test_model, test_model_endpoint, unlimited_pool
):
    """Key created through POST /projects/<sid>/keys works for /v1/ auth."""
    # Create key via the projects route
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "e2e-key", "key": "sk_e2etest1234567890"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    token = resp.get_json()["key"]

    # Use the key to list models
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["object"] == "list"


def test_project_key_lists_accessible_model(
    client, managed_auth_client, managed_project, test_model, test_model_endpoint, unlimited_pool
):
    """Project key sees models it has access to."""
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "model-key", "key": "sk_modelkey12345678"},
    )
    token = resp.get_json()["key"]

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK
    ids = [m["id"] for m in resp.get_json()["data"]]
    assert test_model["model_name"] in ids


def test_project_key_blocked_after_soft_delete(
    client, managed_auth_client, managed_project, test_model_endpoint, unlimited_pool
):
    """Key deactivated via DELETE /projects/<sid>/keys/<kid> returns 401."""
    # Create key
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "del-key", "key": "sk_deletekey12345678"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    token, kid = data["key"], data["id"]

    # Confirm it works
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK

    # Soft-delete the key
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{kid}")
    assert resp.status_code == HTTPStatus.NO_CONTENT

    # Now it should be rejected
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


def test_project_key_no_pool_returns_403(
    client, managed_auth_client, managed_project, test_model, test_model_endpoint
):
    """Project key with no coin pool is denied on chat completions (no EntityLimit → 403)."""
    # No pool granted — service entity has no EntityLimit

    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "nopool-key", "key": "sk_nopoolkey12345678"},
    )
    token = resp.get_json()["key"]

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": test_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
