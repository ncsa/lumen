"""Tests for the admin model access (owner) API."""
from http import HTTPStatus

import pytest
from sqlalchemy import select


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
        from tests.conftest import make_project
        p = make_project("Project", email="project@example.com")
        return {"id": p.id, "email": p.email}


def test_get_access_public_model(admin_client, test_model):
    resp = admin_client.get(f"/admin/api/models/{test_model['id']}/access")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == {"owner": None}


def test_get_access_owned_model(app, admin_client, test_model, test_user):
    with app.app_context():
        from tests.conftest import set_model_owner
        set_model_owner(test_model["id"], test_user["id"])
    resp = admin_client.get(f"/admin/api/models/{test_model['id']}/access")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["owner"]["id"] == test_user["id"]
    assert data["owner"]["email"] == "testuser@example.com"
    assert set(data) == {"owner"}


def test_patch_sets_owner_case_insensitive(app, admin_client, test_model, test_user):
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "TestUser@Example.COM"},
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["owner"]["id"] == test_user["id"]
    assert _owner_id(app, test_model["id"]) == test_user["id"]


def test_patch_ignores_group_ids(app, admin_client, test_model, test_user, group_id):
    """group_ids is retired: a request that still sends it succeeds and the field is ignored."""
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "testuser@example.com", "group_ids": [group_id, 99999]},
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == {
        "owner": {"id": test_user["id"], "name": test_user["name"], "email": "testuser@example.com"},
    }
    assert _owner_id(app, test_model["id"]) == test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_group_access import ModelGroupAccess
        assert db.session.execute(
            select(ModelGroupAccess).filter_by(model_config_id=test_model["id"])
        ).first() is None


def test_patch_without_owner_email_leaves_owner(app, admin_client, test_model, test_user):
    with app.app_context():
        from tests.conftest import set_model_owner
        set_model_owner(test_model["id"], test_user["id"])
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"group_ids": []},
    )
    assert resp.status_code == HTTPStatus.OK
    assert _owner_id(app, test_model["id"]) == test_user["id"]


def test_patch_blank_owner_makes_model_public(app, admin_client, test_model, test_user):
    with app.app_context():
        from tests.conftest import set_model_owner
        set_model_owner(test_model["id"], test_user["id"])
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": ""},
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["owner"] is None
    assert _owner_id(app, test_model["id"]) is None


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

