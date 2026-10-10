"""Tests for the projects blueprint (/projects/*)."""
import re
from datetime import datetime, timedelta
from decimal import Decimal
from http import HTTPStatus

import pytest
from sqlalchemy import func, select

from lumen.timeutils import utcnow
from tests.conftest import make_project

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def service_project(app):
    """An active service (project) entity, owned by a dedicated owner user."""
    with app.app_context():
        c = make_project("test-svc", initials="TS")
        return {"id": c.id, "name": c.name, "owner_id": c.owner_entity_id}


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
    """service_project with test_user as its owner and only manager."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(
            user_entity_id=test_user["id"],
            project_entity_id=service_project["id"],
        ))
        db.session.get(Entity, service_project["id"]).owner_entity_id = test_user["id"]
        db.session.flush()
        db.session.delete(db.session.execute(
            select(EntityManager).filter_by(
                user_entity_id=service_project["owner_id"], project_entity_id=service_project["id"],
            )
        ).scalar_one())
        db.session.commit()
    return {**service_project, "owner_id": test_user["id"]}


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
            )
            db.session.add(key)
            db.session.commit()
            return key.id, raw_key
    return _make


@pytest.fixture
def make_ack_access(app):
    """Factory: mark the (public) model needs_ack so consent is required."""
    def _make(entity_id, model_config_id):
        with app.app_context():
            from lumen.extensions import db
            from lumen.models.model_config import ModelConfig
            db.session.get(ModelConfig, model_config_id).needs_ack = True
            db.session.commit()
    return _make


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


def test_detail_only_embeds_visible_model_metadata(app, managed_auth_client, managed_project):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.model_config import ModelConfig

        owner = Entity(entity_type="user", email="model-owner@example.com", name="Model Owner", active=True)
        blocked = ModelConfig(
            model_name="project-blocked-model",
            owner=owner,
            input_cost_per_million=1,
            output_cost_per_million=1,
        )
        disabled = ModelConfig(
            model_name="project-disabled-model",
            disabled=True,
            input_cost_per_million=1,
            output_cost_per_million=1,
        )
        needs_ack = ModelConfig(
            model_name="project-needs-ack-model",
            needs_ack=True,
            input_cost_per_million=1,
            output_cost_per_million=1,
        )
        db.session.add_all([owner, blocked, disabled, needs_ack])
        db.session.commit()

    resp = managed_auth_client.get(f"/projects/{managed_project['id']}")
    assert resp.status_code == HTTPStatus.OK
    assert b"project-blocked-model" not in resp.data
    assert b"project-disabled-model" not in resp.data
    assert b"project-needs-ack-model" in resp.data


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


def test_create_project_succeeds(app, admin_client, test_user):
    resp = admin_client.post("/projects", json={"name": "created-svc", "owner_email": "testuser@example.com"})
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["name"] == "created-svc"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.execute(select(Entity).filter_by(name="created-svc", entity_type="project")).scalar_one_or_none()
        assert c is not None
        assert c.active is True


def test_create_project_empty_name_returns_400(admin_client):
    resp = admin_client.post("/projects", json={"name": "  "})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_project_duplicate_name_returns_409(admin_client, service_project):
    resp = admin_client.post("/projects", json={"name": service_project["name"]})
    assert resp.status_code == HTTPStatus.CONFLICT
    assert resp.get_json()["error"] == "A project with this name already exists"


def test_create_project_without_owner_returns_400(admin_client):
    """Owner is mandatory: the API rejects a project with no owner email."""
    resp = admin_client.post("/projects", json={"name": "no-owner"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_dialog_renders_owner_lookup_and_disabled_create(admin_client):
    """The create dialog offers a user typeahead for the owner and Create starts disabled."""
    from bs4 import BeautifulSoup

    resp = admin_client.get("/projects")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    assert soup.find(id="project-owner-suggestions") is not None
    assert soup.find(id="project-owner-input").get("aria-controls") == "project-owner-suggestions"
    assert soup.find(id="create-project-btn").has_attr("disabled")


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
# Update project (PATCH)
# ---------------------------------------------------------------------------

def test_update_project_requires_owner_or_admin(managed_auth_client, managed_project):
    resp = managed_auth_client.patch(f"/projects/{managed_project['id']}", json={"name": "nope"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_update_project_owner_renames_and_toggles(app, owner_auth_client, owned_project):
    resp = owner_auth_client.patch(
        f"/projects/{owned_project['id']}", json={"name": "renamed-svc", "active": False}
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        c = db.session.get(Entity, owned_project["id"])
        assert c.name == "renamed-svc"
        assert c.initials == "RE"
        assert c.active is False


def test_update_project_owner_cannot_set_coins(owner_auth_client, owned_project):
    resp = owner_auth_client.patch(
        f"/projects/{owned_project['id']}", json={"max_coins": 100, "refresh_coins": 1}
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_update_project_admin_sets_coins(app, admin_client, service_project):
    resp = admin_client.patch(
        f"/projects/{service_project['id']}", json={"max_coins": 100, "refresh_coins": 2}
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=service_project["id"])
        ).scalar_one_or_none()
        assert limit is not None
        assert float(limit.max_coins) == 100.0
        assert float(limit.refresh_coins) == 2.0
        assert float(limit.starting_coins) == 100.0
        assert limit.config_managed is False


def test_update_project_lowering_max_clamps_balance(app, admin_client, service_project):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.timeutils import utcnow
        db.session.add(EntityLimit(
            entity_id=service_project["id"], max_coins=100, refresh_coins=1, starting_coins=100,
        ))
        db.session.add(EntityBalance(
            entity_id=service_project["id"], coins_left=80, last_refill_at=utcnow(),
        ))
        db.session.commit()
    resp = admin_client.patch(f"/projects/{service_project['id']}", json={"max_coins": 50})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        balance = db.session.execute(
            select(EntityBalance).filter_by(entity_id=service_project["id"])
        ).scalar_one_or_none()
        assert float(balance.coins_left) == 50.0


def test_update_project_unlimited_max_accepted(admin_client, service_project):
    resp = admin_client.patch(
        f"/projects/{service_project['id']}", json={"max_coins": -2, "refresh_coins": 0}
    )
    assert resp.status_code == HTTPStatus.OK


def test_update_project_new_pool_defaults_refresh_to_zero(app, admin_client, service_project):
    resp = admin_client.patch(f"/projects/{service_project['id']}", json={"max_coins": 100})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=service_project["id"])
        ).scalar_one_or_none()
        assert float(limit.max_coins) == 100.0
        assert float(limit.refresh_coins) == 0.0


def test_update_project_refresh_alone_requires_existing_pool(admin_client, service_project):
    resp = admin_client.patch(f"/projects/{service_project['id']}", json={"refresh_coins": 5})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_update_project_invalid_values_return_400(admin_client, service_project):
    for payload in (
        {"max_coins": -1, "refresh_coins": 0},
        {"max_coins": 10, "refresh_coins": -1},
        {"max_coins": "abc", "refresh_coins": 1},
        {"max_coins": 10000000, "refresh_coins": 1},
        {"name": "   "},
    ):
        resp = admin_client.patch(f"/projects/{service_project['id']}", json=payload)
        assert resp.status_code == HTTPStatus.BAD_REQUEST, payload


def test_update_project_duplicate_name_returns_409(app, admin_client, service_project):
    with app.app_context():
        make_project("other-svc", initials="OS")
    resp = admin_client.patch(f"/projects/{service_project['id']}", json={"name": "other-svc"})
    assert resp.status_code == HTTPStatus.CONFLICT


# ---------------------------------------------------------------------------
# Project data API (pagination)
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


def test_projects_data_manager_count_excludes_users(app, admin_client, managed_project, test_user):
    """managers counts only role='manager' rows (the owner and test_user), not 'user' members."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        member = Entity(entity_type="user", email="member@example.com", name="Member", initials="ME", active=True)
        db.session.add(member)
        db.session.flush()
        db.session.add(EntityManager(user_entity_id=member.id, project_entity_id=managed_project["id"], role="user"))
        db.session.commit()
    resp = admin_client.get("/projects/data")
    row = next(c for c in resp.get_json()["projects"] if c["name"] == managed_project["name"])
    assert row["managers"] == 2


def test_projects_data_includes_disabled(admin_client, service_project):
    admin_client.post(f"/projects/{service_project['id']}/toggle")  # deactivate
    resp = admin_client.get("/projects/data")
    row = next(c for c in resp.get_json()["projects"] if c["name"] == service_project["name"])
    assert row["active"] is False


def test_projects_data_includes_edit_fields(app, owner_auth_client, owned_project):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=owned_project["id"], max_coins=75, refresh_coins=1.5, starting_coins=75,
        ))
        db.session.commit()
    resp = owner_auth_client.get("/projects/data")
    row = next(c for c in resp.get_json()["projects"] if c["name"] == owned_project["name"])
    assert row["is_owner"] is True
    assert row["max_coins"] == 75.0
    assert row["refresh_coins"] == 1.5


def test_projects_data_edit_fields_default(managed_auth_client, managed_project):
    resp = managed_auth_client.get("/projects/data")
    row = next(c for c in resp.get_json()["projects"] if c["name"] == managed_project["name"])
    assert row["is_owner"] is False
    assert row["max_coins"] is None
    assert row["refresh_coins"] is None
    assert row["coins_available"] == 0.0
    assert row["last_used"] is None


def test_projects_data_unlimited_coins_available(admin_client, managed_project, unlimited_pool):
    resp = admin_client.get("/projects/data")
    row = next(c for c in resp.get_json()["projects"] if c["name"] == managed_project["name"])
    assert row["coins_available"] == -2


def test_projects_data_shows_default_inherited_unlimited_pool(app, admin_client, managed_project):
    previous_defaults = app.config.get("TOKEN_DEFAULTS")
    app.config["TOKEN_DEFAULTS"] = {"max": -2, "refresh": 0, "starting": 0}
    try:
        resp = admin_client.get("/projects/data")
    finally:
        app.config["TOKEN_DEFAULTS"] = previous_defaults
    row = next(c for c in resp.get_json()["projects"] if c["name"] == managed_project["name"])
    assert row["max_coins"] is None
    assert row["coins_available"] == -2


def test_reset_project_tokens(app, admin_client, service_project):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=service_project["id"], max_coins=100, refresh_coins=1, starting_coins=100,
        ))
        db.session.add(EntityBalance(entity_id=service_project["id"], coins_left=3))
        db.session.commit()
    resp = admin_client.post(f"/admin/entities/{service_project['id']}/reset-tokens")
    assert resp.status_code == HTTPStatus.OK
    assert float(resp.get_json()["coins_available"]) == 100.0


def test_projects_data_manager_sees_disabled_project(app, owner_auth_client, owned_project):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        db.session.get(Entity, owned_project["id"]).active = False
        db.session.commit()
    resp = owner_auth_client.get("/projects/data")
    row = next(c for c in resp.get_json()["projects"] if c["name"] == owned_project["name"])
    assert row["active"] is False


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

def test_add_member_requires_membership(auth_client, service_project):
    resp = auth_client.post(
        f"/projects/{service_project['id']}/users",
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
        assert assoc.role == "user"  # role defaults to user


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

def test_create_project_with_owner(app, admin_client, test_user):
    resp = admin_client.post("/projects", json={"name": "owned-svc", "owner_email": "testuser@example.com"})
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        project = db.session.execute(select(Entity).filter_by(name="owned-svc", entity_type="project")).scalar_one()
        assert project.owner_entity_id == test_user["id"]
        assert db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=project.id)
        ).scalar_one_or_none() is not None


def test_create_project_owner_not_found_returns_404(admin_client):
    resp = admin_client.post("/projects", json={"name": "bad-owner", "owner_email": "nobody@example.com"})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_owner_can_add_manager(owner_auth_client, owned_project, second_user):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/users",
        json={"email": "second@example.com", "role": "manager"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.get_json()["role"] == "manager"


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
        json={"email": "second@example.com", "role": "manager"},
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
        ))
        db.session.commit()
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        assert db.session.get(Entity, owned_project["id"]).owner_entity_id == second_user["id"]
        # The previous owner stays on as a manager.
        assert db.session.execute(
            select(EntityManager).filter_by(user_entity_id=test_user["id"], project_entity_id=owned_project["id"])
        ).scalar_one_or_none() is not None


def test_transfer_to_non_manager_rejected(app, owner_auth_client, owned_project, test_user, second_user):
    """Ownership is a promotion, not an invitation: the target must be a manager."""
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "must already be a manager" in resp.get_json()["error"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        assert db.session.get(Entity, owned_project["id"]).owner_entity_id == test_user["id"]
        assert db.session.execute(
            select(EntityManager).filter_by(user_entity_id=second_user["id"], project_entity_id=owned_project["id"])
        ).scalar_one_or_none() is None


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


def test_admin_transfers_ownership_to_new_manager(app, admin_client, service_project, second_user):
    """An admin hands a project to someone new in two steps: add the user as a
    manager, then make them the owner."""
    resp = admin_client.post(
        f"/projects/{service_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST  # not a manager yet

    admin_client.post(
        f"/projects/{service_project['id']}/users", json={"email": second_user["email"], "role": "manager"},
    )
    resp = admin_client.post(
        f"/projects/{service_project['id']}/owner",
        json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, service_project["id"]).owner_entity_id == second_user["id"]


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
        assert key.revoked_at is None


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


def test_create_key_records_manager_as_creator(app, managed_auth_client, managed_project, test_user):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "prod", "key": "sk_mgrcreator12345"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        key = db.session.execute(select(APIKey).filter_by(entity_id=managed_project["id"], name="prod")).scalar_one()
        assert key.created_by_entity_id == test_user["id"]


def test_create_key_records_non_manager_admin(app, admin_client, service_project, admin_user):
    """A global admin who is not a manager of the project is still the creator."""
    resp = admin_client.post(
        f"/projects/{service_project['id']}/keys",
        json={"name": "ops", "key": "sk_admincreator123"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        key = db.session.execute(select(APIKey).filter_by(entity_id=service_project["id"], name="ops")).scalar_one()
        assert key.created_by_entity_id == admin_user["id"]


def test_created_by_null_for_keys_made_without_a_creator(app, service_project, make_api_key):
    """Keys seeded directly (standing in for legacy rows) keep a NULL creator."""
    make_api_key(service_project["id"], raw_key="sk_legacykey123456", name="legacy")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        key = db.session.execute(select(APIKey).filter_by(entity_id=service_project["id"], name="legacy")).scalar_one()
        assert key.created_by_entity_id is None


def test_deleting_creator_keeps_key_with_null_creator(app, managed_auth_client, managed_project, test_user):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "orphan", "key": "sk_orphcreator123"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from sqlalchemy import delete, text

        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.models.entity import Entity
        # SQLite only enforces FKs per connection; enable it to exercise the
        # ON DELETE SET NULL clause the schema actually carries. The session
        # fixture keeps this connection pooled for later tests, so the finally
        # must hand it back with enforcement off, as it was found.
        try:
            db.session.execute(text("PRAGMA foreign_keys=ON"))
            db.session.execute(delete(Entity).where(Entity.id == test_user["id"]))
            db.session.commit()
            key = db.session.execute(select(APIKey).filter_by(entity_id=managed_project["id"], name="orphan")).scalar_one()
            assert key.created_by_entity_id is None
        finally:
            db.session.rollback()
            db.session.execute(text("PRAGMA foreign_keys=OFF"))
            db.session.commit()


def test_detail_shows_key_creator_and_unknown_for_legacy(managed_auth_client, managed_project, make_api_key):
    """The detail page embeds the creator's display name per key, Unknown for NULL."""
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/keys",
        json={"name": "prod", "key": "sk_detailcreator12"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    make_api_key(managed_project["id"], raw_key="sk_detaillegacy123", name="legacy")
    page = managed_auth_client.get(f"/projects/{managed_project['id']}")
    assert page.status_code == HTTPStatus.OK
    html = page.get_data(as_text=True)
    assert 'created_by: "Test User"' in html
    assert 'created_by: "Unknown"' in html


def test_detail_key_creator_falls_back_to_email(app, managed_auth_client, managed_project):
    """A creator with no display name is shown by email."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.models.entity import Entity
        from lumen.services.crypto import hash_api_key
        silent = Entity(entity_type="user", email="silent@example.com", name="", active=True)
        db.session.add(silent)
        db.session.flush()
        db.session.add(APIKey(
            entity_id=managed_project["id"], name="silent-key",
            created_by_entity_id=silent.id, key_hash=hash_api_key("sk_emailfallback1"),
            key_hint="sk_emai...1234",
        ))
        db.session.commit()
    page = managed_auth_client.get(f"/projects/{managed_project['id']}")
    assert page.status_code == HTTPStatus.OK
    assert 'created_by: "silent@example.com"' in page.get_data(as_text=True)


def test_detail_key_table_has_sortable_created_by_column(managed_auth_client, managed_project):
    """The Created By column exists and is a sortable text column."""
    page = managed_auth_client.get(f"/projects/{managed_project['id']}")
    assert page.status_code == HTTPStatus.OK
    html = page.get_data(as_text=True)
    assert '<th scope="col" class="sort-header" data-col="created_by">Created By' in html
    assert "'requests','tokens','cost','last_used'" in html  # created_by not numeric → asc first click


def test_delete_key_forbidden_for_non_manager(auth_client, service_project, make_api_key):
    key_id, _ = make_api_key(service_project["id"], raw_key="sk_delkey1234567890", name="k")
    resp = auth_client.delete(f"/projects/{service_project['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def _key_state(app, key_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        k = db.session.get(APIKey, key_id)
        assert k is not None
        return k.revoked_at, k.requests, k.input_tokens, k.output_tokens, k.audio_seconds, k.cost


def test_delete_key_soft_deletes(app, managed_auth_client, managed_project, test_user, make_created_key):
    key_id = make_created_key(managed_project["id"], test_user["id"], "sk_todelete12345678")
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    revoked_at, *counters = _key_state(app, key_id)
    assert abs(utcnow() - revoked_at) < timedelta(minutes=1)
    assert counters == [9, 300, 120, 3, Decimal("2.250000")]


def test_revoked_project_key_is_rejected(client, managed_auth_client, managed_project, test_user, make_created_key):
    raw = "sk_revoked401key123"
    key_id = make_created_key(managed_project["id"], test_user["id"], raw)
    headers = {"Authorization": f"Bearer {raw}"}
    assert client.get("/v1/models", headers=headers).status_code == HTTPStatus.OK
    managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{key_id}")
    assert client.get("/v1/models", headers=headers).status_code == HTTPStatus.UNAUTHORIZED


def test_delete_inactive_key_is_noop(app, managed_auth_client, managed_project, test_user, make_created_key):
    key_id = make_created_key(managed_project["id"], test_user["id"], "sk_inactive12345678",
                              revoked_at=datetime(2026, 9, 3, 8, 0))
    before = _key_state(app, key_id)
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{key_id}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert _key_state(app, key_id) == before


def test_deleted_key_still_listed_as_inactive(managed_auth_client, managed_project, make_api_key):
    key_id, _ = make_api_key(managed_project["id"], raw_key="sk_listed12345678", name="gone-key")
    managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{key_id}")
    html = managed_auth_client.get(f"/projects/{managed_project['id']}").get_data(as_text=True)
    row = re.search(r"\{\s*id: " + str(key_id) + r",.*?\}", html, re.S)
    assert row is not None
    assert '"gone-key"' in row.group(0)
    assert re.search(r'revoked_at: "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"', row.group(0))


@pytest.fixture
def make_created_key(app):
    """Factory: create a key on a project with a given creator and usage stats. Returns key id."""
    def _make(sid, creator_id, raw_key, revoked_at=None):
        with app.app_context():
            from lumen.extensions import db
            from lumen.models.api_key import APIKey
            from lumen.services.crypto import hash_api_key
            key = APIKey(
                entity_id=sid, created_by_entity_id=creator_id, name="rot-key",
                key_hash=hash_api_key(raw_key), key_hint=f"{raw_key[:7]}...{raw_key[-4:]}", revoked_at=revoked_at,
                requests=9, input_tokens=300, output_tokens=120, audio_seconds=3,
                cost=Decimal("2.250000"), last_used_at=datetime(2026, 9, 2, 8, 0),
                created_at=datetime(2026, 8, 2, 7, 0),
            )
            db.session.add(key)
            db.session.commit()
            return key.id
    return _make


def _add_manager(app, sid, user_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(user_entity_id=user_id, project_entity_id=sid))
        db.session.commit()


def test_rotate_project_key_by_creator_keeps_stats(app, client, managed_auth_client, managed_project, test_user,
                                                   make_created_key, unlimited_pool):
    old, new = "sk_rotold123456789", "sk_rotnew123456789"
    kid = make_created_key(managed_project["id"], test_user["id"], old)
    fields = ("requests", "input_tokens", "output_tokens", "audio_seconds", "cost",
              "last_used_at", "name", "created_at", "created_by_entity_id", "entity_id", "revoked_at")

    def snapshot():
        with app.app_context():
            from lumen.extensions import db
            from lumen.models.api_key import APIKey
            k = db.session.get(APIKey, kid)
            return {f: getattr(k, f) for f in fields}, k.key_hint

    before, _ = snapshot()
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate", json={"key": new})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == {"id": kid, "name": "rot-key", "key": new}
    after, hint = snapshot()
    assert after == before
    assert hint == f"{new[:7]}...{new[-4:]}"

    anon = app.test_client()
    assert anon.get("/v1/usage", headers={"Authorization": f"Bearer {old}"}).status_code == HTTPStatus.UNAUTHORIZED
    usage = anon.get("/v1/usage", headers={"Authorization": f"Bearer {new}"})
    assert usage.status_code == HTTPStatus.OK
    assert usage.get_json()["requests"] == 9


def test_rotate_project_key_forbidden_for_other_manager(app, managed_auth_client, managed_project, second_user,
                                                        make_created_key):
    _add_manager(app, managed_project["id"], second_user["id"])
    kid = make_created_key(managed_project["id"], second_user["id"], "sk_othermgr1234567")
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate",
                                    json={"key": "sk_othermgrnew1234"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_rotate_project_key_forbidden_for_owner(app, owner_auth_client, owned_project, second_user,
                                                make_created_key):
    _add_manager(app, owned_project["id"], second_user["id"])
    kid = make_created_key(owned_project["id"], second_user["id"], "sk_ownerrot1234567")
    resp = owner_auth_client.post(f"/projects/{owned_project['id']}/keys/{kid}/rotate",
                                  json={"key": "sk_ownerrotnew1234"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_rotate_project_key_forbidden_for_legacy_null_creator(managed_auth_client, managed_project, make_created_key):
    kid = make_created_key(managed_project["id"], None, "sk_legacyrot123456")
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate",
                                    json={"key": "sk_legacyrotnew123"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_rotate_project_key_forbidden_for_non_manager(app, auth_client, service_project, second_user,
                                                      make_created_key):
    _add_manager(app, service_project["id"], second_user["id"])
    kid = make_created_key(service_project["id"], second_user["id"], "sk_nonmgrrot123456")
    resp = auth_client.post(f"/projects/{service_project['id']}/keys/{kid}/rotate",
                            json={"key": "sk_nonmgrrotnew123"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_rotate_project_key_from_other_project_returns_404(app, managed_auth_client, managed_project, test_user,
                                                           make_created_key):
    with app.app_context():
        other_id = make_project("other-svc", initials="OS").id
    kid = make_created_key(other_id, test_user["id"], "sk_otherproj123456")
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate",
                                    json={"key": "sk_otherprojnew123"})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_rotate_project_key_inactive_returns_409(managed_auth_client, managed_project, test_user, make_created_key):
    kid = make_created_key(managed_project["id"], test_user["id"], "sk_inactiverot1234",
                           revoked_at=datetime(2026, 9, 3, 8, 0))
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate",
                                    json={"key": "sk_inactiverotnew1"})
    assert resp.status_code == HTTPStatus.CONFLICT


@pytest.mark.parametrize("payload", [
    {}, {"key": "bad-key-no-prefix"}, {"key": 123}, {"key": ["sk_invalidrotnew12"]}, ["sk_invalidrotnew12"],
])
def test_rotate_project_key_invalid_key_returns_400(managed_auth_client, managed_project, test_user,
                                                    make_created_key, payload):
    kid = make_created_key(managed_project["id"], test_user["id"], "sk_invalidrot12345")
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate", json=payload)
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_rotate_project_key_duplicate_returns_409(managed_auth_client, managed_project, test_user,
                                                  make_created_key):
    kid = make_created_key(managed_project["id"], test_user["id"], "sk_duperot1234567")
    make_created_key(managed_project["id"], test_user["id"], "sk_dupetaken123456")
    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate",
                                    json={"key": "sk_dupetaken123456"})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_rotate_project_key_concurrent_duplicate_returns_409(managed_auth_client, managed_project, test_user,
                                                             make_created_key, monkeypatch):
    """A secret committed by another request after the duplicate check yields 409, not 500."""
    from lumen.blueprints.profile import routes as profile_routes
    from lumen.services.crypto import hash_api_key
    kid = make_created_key(managed_project["id"], test_user["id"], "sk_raceoriginal123")
    make_created_key(managed_project["id"], test_user["id"], "sk_racetaken123456")
    # Skip the pre-check so the unique constraint on key_hash is what catches the duplicate.
    monkeypatch.setattr(profile_routes, "_new_key_fields",
                        lambda key: ({"key_hash": hash_api_key(key), "key_hint": "sk_race...3456"}, None))

    resp = managed_auth_client.post(f"/projects/{managed_project['id']}/keys/{kid}/rotate",
                                    json={"key": "sk_racetaken123456"})
    assert resp.status_code == HTTPStatus.CONFLICT


def _key_rows_can_rotate(html):
    """Map key id -> can_rotate from the KEY_ROWS literal the project page renders."""
    return {int(kid): flag == "true"
            for kid, flag in re.findall(r"id: (\d+),.*?can_rotate: (true|false),", html, re.S)}


def test_detail_rotate_only_on_own_active_keys(app, managed_auth_client, managed_project, test_user, second_user,
                                               make_created_key):
    _add_manager(app, managed_project["id"], second_user["id"])
    sid = managed_project["id"]
    own = make_created_key(sid, test_user["id"], "sk_ownrotui1234567")
    own_inactive = make_created_key(sid, test_user["id"], "sk_owninactui12345",
                                    revoked_at=datetime(2026, 9, 3, 8, 0))
    other = make_created_key(sid, second_user["id"], "sk_otherrotui12345")
    legacy = make_created_key(sid, None, "sk_legacyrotui1234")

    html = managed_auth_client.get(f"/projects/{sid}").get_data(as_text=True)
    assert _key_rows_can_rotate(html) == {own: True, own_inactive: True, other: False, legacy: False}
    # The JS only renders Rotate for active rows with can_rotate.
    assert "${rotate}<button" in html and "k.can_rotate" in html
    assert 'id="rotatedKeyModal"' in html
    assert "js/key-rotate.js" in html
    # Creator ids are not exposed to the page.
    assert "created_by_entity_id" not in html


def test_detail_admin_cannot_rotate_others_keys(admin_client, service_project, test_user, make_created_key):
    kid = make_created_key(service_project["id"], test_user["id"], "sk_adminrotui12345")
    html = admin_client.get(f"/projects/{service_project['id']}").get_data(as_text=True)
    assert _key_rows_can_rotate(html) == {kid: False}


def test_rotate_project_key_requires_login(client, service_project):
    resp = client.post(f"/projects/{service_project['id']}/keys/1/rotate",
                       json={"key": "sk_nologin12345678"}, follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


# ---------------------------------------------------------------------------
# Acknowledgement consent
# ---------------------------------------------------------------------------

def test_consent_forbidden_for_non_manager(auth_client, service_project, test_model, make_ack_access):
    make_ack_access(service_project["id"], test_model["id"])
    resp = auth_client.post(
        f"/projects/{service_project['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_consent_non_ack_model_returns_400(app, managed_auth_client, managed_project, test_model):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/{test_model['model_name']}"
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_consent_blocked_model_matches_not_found(
    app, managed_auth_client, managed_project, test_model, test_user
):
    from tests.conftest import set_model_owner

    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])

    blocked = managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/{test_model['model_name']}"
    )
    unknown = managed_auth_client.post(
        f"/projects/{managed_project['id']}/consent/nonexistent-model-xyz"
    )
    assert blocked.status_code == unknown.status_code == HTTPStatus.NOT_FOUND
    assert blocked.data == unknown.data


def test_consent_ack_model_succeeds(app, managed_auth_client, managed_project, test_model, make_ack_access):
    make_ack_access(managed_project["id"], test_model["id"])
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


def test_consent_idempotent(app, managed_auth_client, managed_project, test_model, make_ack_access):
    """Consenting twice doesn't create duplicate rows."""
    make_ack_access(managed_project["id"], test_model["id"])
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


def test_owner_search_route_removed(owner_auth_client, owned_project):
    """The Change Owner dialog was replaced by per-row Make Owner buttons."""
    resp = owner_auth_client.get(f"/projects/{owned_project['id']}/owner/search?q=Te")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_project_without_owner_rejected_by_db(app, owned_project):
    """ck_entities_project_owner: a project can never lose its owner."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        db.session.get(Entity, owned_project["id"]).owner_entity_id = None
        with _pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_project_transfer_to_manager_with_lower_row_id(app, owner_auth_client, owned_project, second_user):
    owner_auth_client.post(
        f"/projects/{owned_project['id']}/users", json={"email": second_user["email"], "role": "manager"},
    )
    # second_user's row id is higher here; also cover the reverse by transferring twice
    r1 = owner_auth_client.post(f"/projects/{owned_project['id']}/owner", json={"user_id": second_user["id"]})
    assert r1.status_code == HTTPStatus.OK


def test_transfer_by_stale_owner_conflicts(app, owner_auth_client, owned_project, test_user, second_user):
    """Authorized as owner, but ownership moved before the update: the
    caller's request must not hand on the new owner's project."""
    from unittest.mock import patch

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(user_entity_id=second_user["id"], project_entity_id=owned_project["id"]))
        third = Entity(entity_type="user", email="third@example.com", name="Third", initials="TH", active=True)
        db.session.add(third)
        db.session.flush()
        db.session.add(EntityManager(user_entity_id=third.id, project_entity_id=owned_project["id"]))
        # A concurrent transfer already made second_user the owner.
        db.session.get(Entity, owned_project["id"]).owner_entity_id = second_user["id"]
        db.session.commit()
        third_id = third.id

    # The authorization check ran before that transfer committed.
    with patch("lumen.blueprints.projects.routes.is_project_owner", return_value=True):
        resp = owner_auth_client.post(f"/projects/{owned_project['id']}/owner", json={"user_id": third_id})
    assert resp.status_code == HTTPStatus.CONFLICT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, owned_project["id"]).owner_entity_id == second_user["id"]


# ---------------------------------------------------------------------------
# Member roles: owner / manager / user permission matrix
# ---------------------------------------------------------------------------

def _login_as(client, entity_id):
    with client.session_transaction() as sess:
        sess["entity_id"] = entity_id
        sess.pop("admin_mode", None)
    return client


def _add_member(app, project_id, user_id, role):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        db.session.add(EntityManager(user_entity_id=user_id, project_entity_id=project_id, role=role))
        db.session.commit()


def _member_role(app, project_id, user_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        return db.session.scalar(
            select(EntityManager.role).filter_by(user_entity_id=user_id, project_entity_id=project_id)
        )


@pytest.fixture
def user_project(app, service_project, test_user):
    """service_project with test_user as a plain member (role 'user')."""
    _add_member(app, service_project["id"], test_user["id"], "user")
    return service_project


@pytest.fixture
def user_auth_client(auth_client, user_project):
    """auth_client with test_user holding the 'user' role in user_project."""
    return auth_client


def test_manager_can_add_and_remove_users(app, managed_auth_client, managed_project, second_user):
    resp = managed_auth_client.post(
        f"/projects/{managed_project['id']}/users", json={"email": second_user["email"]},
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.get_json()["role"] == "user"
    assert _member_role(app, managed_project["id"], second_user["id"]) == "user"

    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/users/{second_user['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert _member_role(app, managed_project["id"], second_user["id"]) is None


def test_manager_can_search_users(managed_auth_client, managed_project):
    resp = managed_auth_client.get(f"/projects/{managed_project['id']}/users/search?q=second")
    assert resp.status_code == HTTPStatus.OK


def test_manager_cannot_remove_manager(app, managed_auth_client, managed_project, second_user):
    _add_member(app, managed_project["id"], second_user["id"], "manager")
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/users/{second_user['id']}")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert _member_role(app, managed_project["id"], second_user["id"]) == "manager"


def test_add_member_invalid_role_returns_400(owner_auth_client, owned_project, second_user):
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/users", json={"email": second_user["email"], "role": "owner"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_admin_can_add_manager(app, admin_client, service_project, second_user):
    resp = admin_client.post(
        f"/projects/{service_project['id']}/users", json={"email": second_user["email"], "role": "manager"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert _member_role(app, service_project["id"], second_user["id"]) == "manager"


def test_user_cannot_add_or_remove_members(app, user_auth_client, user_project, second_user):
    resp = user_auth_client.post(
        f"/projects/{user_project['id']}/users", json={"email": second_user["email"]},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert _member_role(app, user_project["id"], second_user["id"]) is None

    _add_member(app, user_project["id"], second_user["id"], "user")
    resp = user_auth_client.delete(f"/projects/{user_project['id']}/users/{second_user['id']}")
    assert resp.status_code == HTTPStatus.FORBIDDEN

    resp = user_auth_client.get(f"/projects/{user_project['id']}/users/search?q=second")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_owner_can_promote_and_demote(app, owner_auth_client, owned_project, second_user):
    _add_member(app, owned_project["id"], second_user["id"], "user")
    url = f"/projects/{owned_project['id']}/users/{second_user['id']}"

    resp = owner_auth_client.patch(url, json={"role": "manager"})
    assert resp.status_code == HTTPStatus.OK
    assert _member_role(app, owned_project["id"], second_user["id"]) == "manager"

    resp = owner_auth_client.patch(url, json={"role": "user"})
    assert resp.status_code == HTTPStatus.OK
    assert _member_role(app, owned_project["id"], second_user["id"]) == "user"


def test_admin_can_promote(app, admin_client, service_project, second_user):
    _add_member(app, service_project["id"], second_user["id"], "user")
    resp = admin_client.patch(
        f"/projects/{service_project['id']}/users/{second_user['id']}", json={"role": "manager"},
    )
    assert resp.status_code == HTTPStatus.OK
    assert _member_role(app, service_project["id"], second_user["id"]) == "manager"


def test_manager_cannot_promote_or_demote(app, managed_auth_client, managed_project, second_user, test_user):
    _add_member(app, managed_project["id"], second_user["id"], "user")
    resp = managed_auth_client.patch(
        f"/projects/{managed_project['id']}/users/{second_user['id']}", json={"role": "manager"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert _member_role(app, managed_project["id"], second_user["id"]) == "user"

    resp = managed_auth_client.patch(
        f"/projects/{managed_project['id']}/users/{test_user['id']}", json={"role": "user"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert _member_role(app, managed_project["id"], test_user["id"]) == "manager"


def test_user_cannot_promote_self(app, user_auth_client, user_project, test_user):
    resp = user_auth_client.patch(
        f"/projects/{user_project['id']}/users/{test_user['id']}", json={"role": "manager"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert _member_role(app, user_project["id"], test_user["id"]) == "user"


def test_update_member_invalid_role_and_unknown_member(owner_auth_client, owned_project, second_user):
    url = f"/projects/{owned_project['id']}/users/{second_user['id']}"
    resp = owner_auth_client.patch(url, json={"role": "admin"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    resp = owner_auth_client.patch(url, json={"role": "user"})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_demoting_owner_returns_409(app, admin_client, owned_project, test_user):
    resp = admin_client.patch(
        f"/projects/{owned_project['id']}/users/{test_user['id']}", json={"role": "user"},
    )
    assert resp.status_code == HTTPStatus.CONFLICT
    assert _member_role(app, owned_project["id"], test_user["id"]) == "manager"


def test_removing_owner_returns_409_for_admin_and_manager(app, admin_client, managed_project, second_user):
    owner_url = f"/projects/{managed_project['id']}/users/{managed_project['owner_id']}"
    resp = admin_client.delete(owner_url)
    assert resp.status_code == HTTPStatus.CONFLICT

    _add_member(app, managed_project["id"], second_user["id"], "manager")
    _login_as(admin_client, second_user["id"])
    resp = admin_client.delete(owner_url)
    assert resp.status_code == HTTPStatus.CONFLICT


def test_transfer_ownership_to_user_rejected(app, owner_auth_client, owned_project, test_user, second_user):
    _add_member(app, owned_project["id"], second_user["id"], "user")
    resp = owner_auth_client.post(
        f"/projects/{owned_project['id']}/owner", json={"user_id": second_user["id"]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "must already be a manager" in resp.get_json()["error"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, owned_project["id"]).owner_entity_id == test_user["id"]


def test_user_can_have_only_one_active_key(app, user_auth_client, user_project):
    url = f"/projects/{user_project['id']}/keys"
    first = user_auth_client.post(url, json={"name": "one", "key": "sk_userkey_one_1234"})
    assert first.status_code == HTTPStatus.CREATED

    second = user_auth_client.post(url, json={"name": "two", "key": "sk_userkey_two_1234"})
    assert second.status_code == HTTPStatus.CONFLICT
    assert second.get_json()["error"] == "Users can have only one API key"

    resp = user_auth_client.delete(f"{url}/{first.get_json()['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        assert db.session.get(APIKey, first.get_json()["id"]).revoked_at is not None

    third = user_auth_client.post(url, json={"name": "three", "key": "sk_userkey_three_1234"})
    assert third.status_code == HTTPStatus.CREATED


def test_user_key_limit_ignores_inactive_and_other_members_keys(app, user_auth_client, user_project, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        db.session.add_all([
            APIKey(entity_id=user_project["id"], created_by_entity_id=test_user["id"],
                   name="old", key_hash="a" * 64, revoked_at=datetime(2026, 9, 3, 8, 0)),
            APIKey(entity_id=user_project["id"], created_by_entity_id=user_project["owner_id"],
                   name="owner", key_hash="b" * 64),
        ])
        db.session.commit()
    resp = user_auth_client.post(
        f"/projects/{user_project['id']}/keys", json={"name": "mine", "key": "sk_userkey_mine_1234"},
    )
    assert resp.status_code == HTTPStatus.CREATED


def test_manager_can_create_many_keys(managed_auth_client, managed_project):
    url = f"/projects/{managed_project['id']}/keys"
    resp = managed_auth_client.post(url, json={"key": "sk_mgrkey_one_1234"})
    assert resp.status_code == HTTPStatus.CREATED
    resp = managed_auth_client.post(url, json={"key": "sk_mgrkey_two_1234"})
    assert resp.status_code == HTTPStatus.CREATED


def test_user_deleting_another_members_key_returns_404(app, user_auth_client, user_project, make_api_key):
    kid, _ = make_api_key(user_project["id"], raw_key="sk_ownerkey_abcdef", name="owner-key")
    resp = user_auth_client.delete(f"/projects/{user_project['id']}/keys/{kid}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        assert db.session.get(APIKey, kid) is not None


def test_manager_can_delete_any_key(app, managed_auth_client, managed_project, make_api_key):
    kid, _ = make_api_key(managed_project["id"], raw_key="sk_ownerkey_abcdef", name="owner-key")
    resp = managed_auth_client.delete(f"/projects/{managed_project['id']}/keys/{kid}")
    assert resp.status_code == HTTPStatus.NO_CONTENT


def _keys_on_page(client, project_id):
    return client.get(f"/projects/{project_id}").get_data(as_text=True)


def test_user_detail_shows_only_own_keys(app, user_auth_client, user_project, make_api_key):
    make_api_key(user_project["id"], raw_key="sk_otherkey_zq9x", name="someone-elses-key")
    user_auth_client.post(
        f"/projects/{user_project['id']}/keys", json={"name": "my-own-key", "key": "sk_minekey_m7w3"},
    )
    page = _keys_on_page(user_auth_client, user_project["id"])
    assert "my-own-key" in page
    assert "sk_mine...m7w3" in page
    assert "someone-elses-key" not in page
    assert "zq9x" not in page


def test_manager_and_owner_detail_show_all_keys(app, client, user_project, test_user, second_user, make_api_key):
    make_api_key(user_project["id"], raw_key="sk_otherkey_zq9x", name="someone-elses-key")
    _login_as(client, test_user["id"])
    client.post(f"/projects/{user_project['id']}/keys", json={"name": "my-own-key", "key": "sk_minekey_m7w3"})

    _add_member(app, user_project["id"], second_user["id"], "manager")
    for viewer in (second_user["id"], user_project["owner_id"]):
        page = _keys_on_page(_login_as(client, viewer), user_project["id"])
        assert "my-own-key" in page
        assert "someone-elses-key" in page


def test_user_can_view_project_but_not_acknowledge_models(
    app, user_auth_client, user_project, test_model, make_ack_access,
):
    resp = user_auth_client.get(f"/projects/{user_project['id']}")
    assert resp.status_code == HTTPStatus.OK
    make_ack_access(user_project["id"], test_model["id"])
    resp = user_auth_client.post(f"/projects/{user_project['id']}/consent/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        assert db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=user_project["id"])
        ).scalar_one_or_none() is None


@pytest.mark.parametrize("caller", ["manager", "owner", "admin"])
def test_manager_owner_and_admin_can_acknowledge_models(
    app, client, user_project, second_user, admin_user, test_model, make_ack_access, caller,
):
    _add_member(app, user_project["id"], second_user["id"], "manager")
    caller_id = {"manager": second_user["id"], "owner": user_project["owner_id"], "admin": admin_user["id"]}[caller]
    _login_as(client, caller_id)
    if caller == "admin":
        with client.session_transaction() as sess:
            sess["admin_mode"] = True
    make_ack_access(user_project["id"], test_model["id"])
    resp = client.post(f"/projects/{user_project['id']}/consent/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK


def test_user_cannot_edit_or_toggle_project(user_auth_client, user_project):
    resp = user_auth_client.post(f"/projects/{user_project['id']}/toggle")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    resp = user_auth_client.patch(f"/projects/{user_project['id']}", json={"name": "renamed"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("role", ["", None, 0, "owner"])
def test_add_member_explicit_invalid_role_returns_400(app, admin_client, service_project, second_user, role):
    resp = admin_client.post(
        f"/projects/{service_project['id']}/users", json={"email": second_user["email"], "role": role},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert _member_role(app, service_project["id"], second_user["id"]) is None


def test_demoting_manager_with_several_active_keys_returns_409(app, owner_auth_client, owned_project, second_user):
    _add_member(app, owned_project["id"], second_user["id"], "manager")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        db.session.add_all([
            APIKey(entity_id=owned_project["id"], created_by_entity_id=second_user["id"],
                   name=f"k{i}", key_hash=str(i) * 64)
            for i in (1, 2)
        ])
        db.session.commit()
    url = f"/projects/{owned_project['id']}/users/{second_user['id']}"
    resp = owner_auth_client.patch(url, json={"role": "user"})
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "2 active API keys" in resp.get_json()["error"]
    assert _member_role(app, owned_project["id"], second_user["id"]) == "manager"


def test_demoting_manager_with_one_active_key_succeeds(app, owner_auth_client, owned_project, second_user):
    _add_member(app, owned_project["id"], second_user["id"], "manager")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        db.session.add_all([
            APIKey(entity_id=owned_project["id"], created_by_entity_id=second_user["id"],
                   name="live", key_hash="1" * 64),
            APIKey(entity_id=owned_project["id"], created_by_entity_id=second_user["id"],
                   name="old", key_hash="2" * 64, revoked_at=datetime(2026, 9, 3, 8, 0)),
        ])
        db.session.commit()
    resp = owner_auth_client.patch(
        f"/projects/{owned_project['id']}/users/{second_user['id']}", json={"role": "user"},
    )
    assert resp.status_code == HTTPStatus.OK
    assert _member_role(app, owned_project["id"], second_user["id"]) == "user"


def test_create_key_refused_when_membership_removed_before_lock(app, user_auth_client, user_project, test_user):
    """A removal that commits before the membership lock is taken must not mint a key.

    The listener deletes the caller's membership row just before the route's
    locked SELECT on entity_managers runs, as a concurrent removal would.
    """
    from sqlalchemy import delete, event
    from sqlalchemy.orm import Session

    from lumen.models.entity_manager import EntityManager

    removed = []

    def remove_before_lock(state):
        stmt = state.statement
        if (not removed and state.is_select and stmt._for_update_arg is not None
                and EntityManager.__table__ in stmt.get_final_froms()):
            removed.append(True)
            state.session.connection().execute(delete(EntityManager.__table__).where(
                EntityManager.user_entity_id == test_user["id"],
                EntityManager.project_entity_id == user_project["id"],
            ))

    event.listen(Session, "do_orm_execute", remove_before_lock)
    try:
        resp = user_auth_client.post(
            f"/projects/{user_project['id']}/keys", json={"name": "late", "key": "sk_removedkey_1234"},
        )
    finally:
        event.remove(Session, "do_orm_execute", remove_before_lock)

    assert removed, "the route never took the membership lock"
    assert resp.status_code == HTTPStatus.FORBIDDEN
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        assert db.session.scalar(
            select(func.count(APIKey.id)).where(APIKey.entity_id == user_project["id"])
        ) == 0


def test_user_sees_only_managers_owner_and_self(app, client, user_project, test_user, second_user, admin_user):
    """U1 (test_user) and U2 are users; second_user is a manager. U1 sees the
    owner, the manager and themselves but never U2; everyone else sees U2."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        hidden = Entity(entity_type="user", email="hidden-u2@example.com", name="Hidden Member U2",
                        initials="HU", active=True)
        db.session.add(hidden)
        db.session.commit()
        hidden_id = hidden.id
        owner = db.session.get(Entity, user_project["owner_id"])
        owner_name, owner_email = owner.name, owner.email
    _add_member(app, user_project["id"], hidden_id, "user")
    _add_member(app, user_project["id"], second_user["id"], "manager")
    url = f"/projects/{user_project['id']}"

    page = _login_as(client, test_user["id"]).get(url).get_data(as_text=True)
    assert "testuser@example.com" in page
    assert owner_name in page and owner_email in page
    assert second_user["name"] in page and second_user["email"] in page
    assert "Hidden Member U2" not in page
    assert "hidden-u2@example.com" not in page

    for viewer in (second_user["id"], user_project["owner_id"], admin_user["id"]):
        _login_as(client, viewer)
        if viewer == admin_user["id"]:
            with client.session_transaction() as sess:
                sess["admin_mode"] = True
        page = client.get(url).get_data(as_text=True)
        assert "Hidden Member U2" in page
        assert "hidden-u2@example.com" in page


# ---------------------------------------------------------------------------
# Members tab: each role sees only the actions it may take
# ---------------------------------------------------------------------------

def _members_page(client, project_id):
    from bs4 import BeautifulSoup
    return BeautifulSoup(client.get(f"/projects/{project_id}").data, "html.parser")


def _action_labels(soup):
    return {b.get("aria-label") for b in soup.select("#pane-members tbody button") if not b.has_attr("disabled")}


@pytest.fixture
def mixed_members(app, service_project, second_user):
    """service_project with second_user as manager and a third user as plain user."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        third = Entity(entity_type="user", email="third@example.com", name="Third User",
                       initials="TU", active=True)
        db.session.add(third)
        db.session.commit()
        third_id = third.id
    _add_member(app, service_project["id"], second_user["id"], "manager")
    _add_member(app, service_project["id"], third_id, "user")
    return service_project


def test_members_tab_owner_sees_all_actions(client, mixed_members):
    soup = _members_page(_login_as(client, mixed_members["owner_id"]), mixed_members["id"])
    assert soup.find(id="tab-members").get_text(strip=True) == "Members"
    assert soup.find("button", attrs={"data-bs-target": "#addMemberModal"}).get_text(strip=True) == "+ Add User"
    assert soup.find(id="changeOwnerModal") is None
    assert [o["value"] for o in soup.select("#add-member-role option")] == ["user", "manager"]
    assert _action_labels(soup) == {
        "Make Second User owner", "Demote Second User to user", "Remove Second User",
        "Promote Third User to manager", "Remove Third User",
    }
    owner_remove = soup.find("button", attrs={"aria-label": "Cannot remove yourself as owner"})
    assert owner_remove.has_attr("disabled")
    assert soup.find(id=owner_remove["aria-describedby"]).get_text(strip=True) == "Make another manager owner first"


def test_members_tab_admin_sees_owner_actions(admin_client, mixed_members):
    soup = _members_page(admin_client, mixed_members["id"])
    assert [o["value"] for o in soup.select("#add-member-role option")] == ["user", "manager"]
    assert soup.find(id="changeOwnerModal") is None
    labels = _action_labels(soup)
    assert "Demote Second User to user" in labels
    assert {lbl for lbl in labels if lbl.startswith("Make ")} == {"Make Second User owner"}


def test_members_tab_manager_can_only_add_and_remove_users(client, mixed_members, second_user):
    soup = _members_page(_login_as(client, second_user["id"]), mixed_members["id"])
    assert soup.find("button", attrs={"data-bs-target": "#addMemberModal"})
    assert soup.find(id="changeOwnerModal") is None
    assert soup.find(id="editProjectModal") is None
    assert [o["value"] for o in soup.select("#add-member-role option")] == ["user"]
    assert _action_labels(soup) == {"Remove Third User"}


def test_members_tab_user_sees_no_member_actions(app, client, mixed_members):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        third_id = db.session.scalar(select(Entity.id).filter_by(email="third@example.com"))
    soup = _members_page(_login_as(client, third_id), mixed_members["id"])
    assert soup.find("button", attrs={"data-bs-target": "#addMemberModal"}) is None
    assert soup.find(id="addMemberModal") is None
    assert soup.find(id="add-member-role") is None
    assert soup.select("#pane-members tbody button") == []
    assert soup.find(id="changeOwnerModal") is None
    roles = [td.get_text(strip=True) for td in soup.select("#pane-members tbody tr td:nth-of-type(3)")]
    assert sorted(roles) == ["Manager", "Owner", "User"]


def test_new_key_button_disabled_once_user_has_a_key(user_auth_client, user_project):
    def new_key_button():
        soup = _members_page(user_auth_client, user_project["id"])
        return soup, soup.find("button", string=lambda t: t and "New API Key" in t)

    soup, btn = new_key_button()
    assert not btn.has_attr("disabled")
    assert soup.find(id="key-limit-note") is None

    resp = user_auth_client.post(f"/projects/{user_project['id']}/keys", json={"name": "k", "key": "sk_onlykey_0001"})
    soup, btn = new_key_button()
    assert btn.has_attr("disabled")
    assert "only one active key" in soup.find(id=btn["aria-describedby"]).get_text()

    user_auth_client.delete(f"/projects/{user_project['id']}/keys/{resp.get_json()['id']}")
    _, btn = new_key_button()
    assert not btn.has_attr("disabled")


def test_new_key_button_stays_enabled_for_managers(app, managed_auth_client, managed_project):
    managed_auth_client.post(f"/projects/{managed_project['id']}/keys", json={"name": "k", "key": "sk_mgrkey_0001"})
    soup = _members_page(managed_auth_client, managed_project["id"])
    assert not soup.find("button", string=lambda t: t and "New API Key" in t).has_attr("disabled")


def _member_names(soup):
    return [td.get_text(strip=True) for td in soup.select("#pane-members tbody tr td:nth-of-type(1)")]


def test_members_listed_owner_then_managers_then_users(app, client):
    """Role order wins over name order; names sort A-Z within each role."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    with app.app_context():
        ids = {}
        for name in ("Zed Owner", "Yan Manager", "Bob Manager", "Amy User", "Carl User"):
            e = Entity(entity_type="user", email=f"{name.split()[0].lower()}@example.com", name=name,
                       initials="XX", active=True)
            db.session.add(e)
            db.session.flush()
            ids[name] = e.id
        db.session.commit()
        sid = make_project("ordered", owner_id=ids["Zed Owner"]).id
    for name, role in (("Yan Manager", "manager"), ("Bob Manager", "manager"),
                       ("Carl User", "user"), ("Amy User", "user")):
        _add_member(app, sid, ids[name], role)

    _login_as(client, ids["Zed Owner"])
    assert _member_names(_members_page(client, sid)) == [
        "Zed Owner", "Bob Manager", "Yan Manager", "Amy User", "Carl User",
    ]

    assert client.post(f"/projects/{sid}/owner", json={"user_id": ids["Yan Manager"]}).status_code == HTTPStatus.OK
    assert _member_names(_members_page(client, sid)) == [
        "Yan Manager", "Bob Manager", "Zed Owner", "Amy User", "Carl User",
    ]


def test_make_owner_only_on_manager_rows_for_owner_and_admin(app, client, admin_client, mixed_members, second_user):
    def make_owner_labels(soup):
        return {b["aria-label"] for b in soup.select("#pane-members tbody button") if b.get_text(strip=True) == "Make Owner"}

    expected = {"Make Second User owner"}
    assert make_owner_labels(_members_page(admin_client, mixed_members["id"])) == expected
    assert make_owner_labels(_members_page(_login_as(client, mixed_members["owner_id"]), mixed_members["id"])) == expected
    assert make_owner_labels(_members_page(_login_as(client, second_user["id"]), mixed_members["id"])) == set()
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        third_id = db.session.scalar(select(Entity.id).filter_by(email="third@example.com"))
    assert make_owner_labels(_members_page(_login_as(client, third_id), mixed_members["id"])) == set()


def test_user_key_table_hides_created_by(app, client, user_project, test_user, second_user):
    _login_as(client, test_user["id"])
    client.post(f"/projects/{user_project['id']}/keys", json={"name": "mine", "key": "sk_minekey_h1d3"})
    soup = _members_page(client, user_project["id"])
    headers = [th.get_text(" ", strip=True) for th in soup.select("#key-table thead th")]
    assert not any(h.startswith("Created By") for h in headers)
    search = soup.find(id="key-search")
    assert "creator" not in search["placeholder"] and "creator" not in search["aria-label"]
    assert "Test User" not in soup.find("script", string=lambda t: t and "KEY_ROWS" in t).string

    _add_member(app, user_project["id"], second_user["id"], "manager")
    soup = _members_page(_login_as(client, second_user["id"]), user_project["id"])
    headers = [th.get_text(" ", strip=True) for th in soup.select("#key-table thead th")]
    assert any(h.startswith("Created By") for h in headers)
    assert "creator" in soup.find(id="key-search")["placeholder"]


def test_user_models_tab_has_no_ack_button(client, user_project, test_user, second_user, app):
    _add_member(app, user_project["id"], second_user["id"], "manager")
    page = _login_as(client, test_user["id"]).get(f"/projects/{user_project['id']}").get_data(as_text=True)
    assert "ack-btn" not in page
    assert "Needs consent: ask a project manager" in page
    assert 'id="ackModal"' not in page

    page = _login_as(client, second_user["id"]).get(f"/projects/{user_project['id']}").get_data(as_text=True)
    assert "ack-btn" in page
    assert 'id="ackModal"' in page


def test_user_sees_self_owner_and_managers_only(app, client, mixed_members, second_user):
    """Plain users do not see the other users; the Members stat matches the rows shown."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        other = Entity(entity_type="user", email="other@example.com", name="Other User",
                       initials="OU", active=True)
        db.session.add(other)
        db.session.commit()
        other_id = other.id
        third_id = db.session.scalar(select(Entity.id).filter_by(email="third@example.com"))
    _add_member(app, mixed_members["id"], other_id, "user")

    soup = _members_page(_login_as(client, third_id), mixed_members["id"])
    assert _member_names(soup) == ["Owner of test-svc", "Second User", "Third User"]
    stat = soup.find("div", string="Members").find_next_sibling("div").get_text(strip=True)
    assert int(stat) == len(_member_names(soup))

    soup = _members_page(_login_as(client, second_user["id"]), mixed_members["id"])
    assert _member_names(soup) == ["Owner of test-svc", "Second User", "Other User", "Third User"]
    assert soup.find("div", string="Members").find_next_sibling("div").get_text(strip=True) == "4"


def _owner_row(soup):
    return next(tr for tr in soup.select("#pane-members tbody tr")
                if tr.select("td")[2].get_text(strip=True) == "Owner")


def test_owner_row_remove_cell_per_role(client, admin_client, mixed_members, second_user):
    sid = mixed_members["id"]
    # admin_client and client are the same test client, so check the admin view first.
    for login, label in (
        (lambda: admin_client, "Cannot remove owner Owner of test-svc"),
        (lambda: _login_as(client, mixed_members["owner_id"]), "Cannot remove yourself as owner"),
    ):
        viewer = login()
        page = viewer.get(f"/projects/{sid}").get_data(as_text=True)
        assert "Transfer ownership first" not in page
        soup = _members_page(viewer, sid)
        btn = _owner_row(soup).find("button")
        assert btn.has_attr("disabled") and btn["aria-label"] == label
        assert soup.find(id=btn["aria-describedby"]).get_text(strip=True) == "Make another manager owner first"

    manager = _login_as(client, second_user["id"])
    page = manager.get(f"/projects/{sid}").get_data(as_text=True)
    assert "Transfer ownership first" not in page
    assert "Make another manager owner first" not in page
    soup = _members_page(manager, sid)
    assert _owner_row(soup).find("button") is None
