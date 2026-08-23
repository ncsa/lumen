"""Tests for the admin model access (owner + group grants) API."""
from http import HTTPStatus

import pytest
from sqlalchemy import select


def _grant_group_ids(app, model_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_group_access import ModelGroupAccess
        return sorted(
            row.group_id for row in db.session.execute(
                select(ModelGroupAccess).filter_by(model_config_id=model_id)
            ).scalars().all()
        )


def _owner_id(app, model_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        return db.session.get(ModelConfig, model_id).owner_entity_id


@pytest.fixture
def group_id(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="access-group")
        db.session.add(g)
        db.session.commit()
        return g.id


@pytest.fixture
def project_entity(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        p = Entity(entity_type="project", email="project@example.com", name="Project", active=True)
        db.session.add(p)
        db.session.commit()
        return {"id": p.id, "email": p.email}


def test_get_access_public_model(admin_client, test_model, group_id):
    resp = admin_client.get(f"/admin/api/models/{test_model['id']}/access")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["owner"] is None
    assert data["granted_group_ids"] == []
    assert {"id": group_id, "name": "access-group", "active": True} in data["groups"]


def test_get_access_owned_model(app, admin_client, test_model, test_user, group_id):
    with app.app_context():
        from tests.conftest import grant_model_to_group, set_model_owner
        set_model_owner(test_model["id"], test_user["id"])
        grant_model_to_group(test_model["id"], group_id)
    resp = admin_client.get(f"/admin/api/models/{test_model['id']}/access")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["owner"]["id"] == test_user["id"]
    assert data["owner"]["email"] == "testuser@example.com"
    assert data["granted_group_ids"] == [group_id]


def test_patch_sets_owner_and_grants_case_insensitive(app, admin_client, test_model, test_user, group_id):
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "TestUser@Example.COM", "group_ids": [group_id]},
    )
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["owner"]["id"] == test_user["id"]
    assert data["granted_group_ids"] == [group_id]
    assert _owner_id(app, test_model["id"]) == test_user["id"]
    assert _grant_group_ids(app, test_model["id"]) == [group_id]


def test_patch_blank_owner_clears_owner_and_grants(app, admin_client, test_model, test_user, group_id):
    with app.app_context():
        from tests.conftest import grant_model_to_group, set_model_owner
        set_model_owner(test_model["id"], test_user["id"])
        grant_model_to_group(test_model["id"], group_id)
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": ""},
    )
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["owner"] is None
    assert data["granted_group_ids"] == []
    assert _owner_id(app, test_model["id"]) is None
    assert _grant_group_ids(app, test_model["id"]) == []


def test_patch_unknown_email_400(admin_client, test_model):
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "nobody@example.com"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "no user with that email"


def test_patch_project_email_400(admin_client, test_model, project_entity):
    """Owner must be a user entity — a project's email is rejected."""
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": project_entity["email"]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "no user with that email"


def test_patch_unknown_group_id_400(app, admin_client, test_model, test_user):
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "testuser@example.com", "group_ids": [99999]},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "unknown group id" in resp.get_json()["error"]
    # The failed PATCH leaves a flushed-but-uncommitted owner update in the
    # ambient session pytest-flask shares with the test client; roll it back
    # so the SQLite write lock is released before the clean_db teardown.
    from lumen.extensions import db
    db.session.rollback()


def test_patch_cannot_add_inactive_group_grant(admin_client, test_model, test_user, group_id):
    toggle = admin_client.post(f"/groups/{group_id}/toggle")
    assert toggle.status_code == HTTPStatus.OK

    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "testuser@example.com", "group_ids": [group_id]},
    )

    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == f"inactive group id(s): {group_id}"


def test_patch_preserves_existing_inactive_group_grant(app, admin_client, test_model, test_user, group_id):
    initial = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "testuser@example.com", "group_ids": [group_id]},
    )
    assert initial.status_code == HTTPStatus.OK
    toggle = admin_client.post(f"/groups/{group_id}/toggle")
    assert toggle.status_code == HTTPStatus.OK

    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"group_ids": [group_id]},
    )

    assert resp.status_code == HTTPStatus.OK
    assert _grant_group_ids(app, test_model["id"]) == [group_id]


def test_patch_group_ids_ignored_without_owner(app, admin_client, test_model, group_id):
    """group_ids on a public model are ignored — a public model carries no grants."""
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "", "group_ids": [group_id]},
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["granted_group_ids"] == []
    assert _grant_group_ids(app, test_model["id"]) == []


def test_patch_replaces_grant_set(app, admin_client, test_model, test_user, group_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from tests.conftest import grant_model_to_group, set_model_owner
        other = Group(name="other-group")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
        set_model_owner(test_model["id"], test_user["id"])
        grant_model_to_group(test_model["id"], other_id)
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"group_ids": [group_id]},
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["granted_group_ids"] == [group_id]
    assert _grant_group_ids(app, test_model["id"]) == [group_id]


def test_patch_non_json_body_400(admin_client, test_model):
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        data="not json",
        content_type="application/json",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_get_missing_model_404(admin_client):
    resp = admin_client.get("/admin/api/models/999999/access")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_access_requires_admin(auth_client, test_model):
    resp = auth_client.get(f"/admin/api/models/{test_model['id']}/access")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    resp = auth_client.patch(
        f"/admin/api/models/{test_model['id']}/access", json={"owner_email": ""}
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_patch_access_rejected_without_csrf_token(app, admin_user, test_model):
    """CSRF protection applies to the access PATCH endpoint."""
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        client = app.test_client(use_cookies=True)
        with client.session_transaction() as sess:
            sess["entity_id"] = admin_user["id"]
            sess["entity_name"] = admin_user["name"]
            sess["initials"] = admin_user["initials"]
            sess["gravatar_hash"] = admin_user["gravatar_hash"]
            sess["admin_mode"] = True
        resp = client.patch(
            f"/admin/api/models/{test_model['id']}/access",
            json={"owner_email": ""},
        )
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert b"CSRF" in resp.data
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_get_omits_ungranted_inactive_groups(app, admin_client, test_model):
    """The dialog offers active groups plus only those inactive groups already granted."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.model_group_access import ModelGroupAccess
        active = Group(name="active-grp", active=True)
        idle = Group(name="idle-grp", active=False)
        idle_granted = Group(name="idle-granted-grp", active=False)
        db.session.add_all([active, idle, idle_granted])
        db.session.flush()
        db.session.add(ModelGroupAccess(model_config_id=test_model["id"], group_id=idle_granted.id))
        db.session.commit()
        active_id, idle_id, idle_granted_id = active.id, idle.id, idle_granted.id

    resp = admin_client.get(f"/admin/api/models/{test_model['id']}/access")
    assert resp.status_code == 200
    ids = {g["id"] for g in resp.get_json()["groups"]}
    assert active_id in ids
    assert idle_granted_id in ids
    assert idle_id not in ids
