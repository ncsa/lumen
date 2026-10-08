"""
Static responsive-markup guard: every Bootstrap data table (``<table class="table">``)
on a server-rendered page must sit inside a ``.table-responsive`` wrapper, so a
wide table scrolls inside its own box instead of pushing the whole page sideways
on a phone. Tables built in JS (e.g. the config editor's endpoints table) are
not in the server HTML and are covered by ``scripts/responsive_check.py`` instead.
"""
from http import HTTPStatus

import pytest
from bs4 import BeautifulSoup

from lumen.extensions import db, limiter
from lumen.models.entity import Entity
from lumen.models.group import Group
from lumen.models.group_member import GroupMember


def _project_url(app, admin_user, test_model):
    with app.app_context():
        project = Entity(entity_type="project", name="resp-svc", initials="RS", active=True)
        db.session.add(project)
        db.session.commit()
        return f"/projects/{project.id}"


def _group_url(app, admin_user, test_model):
    with app.app_context():
        group = Group(name="resp-group", active=True)
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(group_id=group.id, entity_id=admin_user["id"], is_owner=True))
        db.session.commit()
        return f"/groups/{group.id}"


def _consent_url(app, admin_user, test_model):
    limiter.reset()
    body = app.test_client().post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": "opencode", "author": "alice",
    }).get_json()
    return f"/device?code={body['user_code']}"


def _model_url(app, admin_user, test_model):
    return f"/models/{test_model['model_name']}"


# (client fixture, url or url builder, expected status)
PAGES = [
    pytest.param("client", "/", HTTPStatus.OK, id="landing"),
    pytest.param("auth_client", "/chat", HTTPStatus.OK, id="chat"),
    pytest.param("auth_client", "/profile", HTTPStatus.OK, id="profile"),
    pytest.param("auth_client", "/usage", HTTPStatus.OK, id="usage"),
    pytest.param("auth_client", "/models", HTTPStatus.OK, id="models"),
    pytest.param("auth_client", _model_url, HTTPStatus.OK, id="model-detail"),
    pytest.param("auth_client", "/projects", HTTPStatus.OK, id="projects"),
    pytest.param("admin_client", _project_url, HTTPStatus.OK, id="project-detail"),
    pytest.param("auth_client", "/groups", HTTPStatus.OK, id="groups"),
    pytest.param("admin_client", _group_url, HTTPStatus.OK, id="group-detail"),
    pytest.param("auth_client", "/connect", HTTPStatus.OK, id="connect"),
    pytest.param("auth_client", "/help/", HTTPStatus.OK, id="help"),
    pytest.param("admin_client", "/admin/users", HTTPStatus.OK, id="admin-users"),
    pytest.param("admin_client", "/admin/config", HTTPStatus.OK, id="admin-config"),
    # /admin/analytics redirects here; admin mode adds the all-users filters.
    pytest.param("admin_client", "/usage", HTTPStatus.OK, id="admin-analytics"),
    pytest.param("auth_client", _consent_url, HTTPStatus.OK, id="oauth-consent"),
    pytest.param("auth_client", "/device", HTTPStatus.OK, id="oauth-device"),
    pytest.param("auth_client", "/responsive-check-missing", HTTPStatus.NOT_FOUND, id="404"),
]


@pytest.mark.parametrize("client_name, url, status", PAGES)
def test_data_tables_are_responsive(request, app, admin_user, test_model, client_name, url, status):
    client = request.getfixturevalue(client_name)
    if callable(url):
        url = url(app, admin_user, test_model)
    resp = client.get(url)
    assert resp.status_code == status

    soup = BeautifulSoup(resp.data, "html.parser")
    bare = [
        table.get("id") or (table.caption.get_text(strip=True) if table.caption else "<no id>")
        for table in soup.select("table.table")
        if not table.find_parent(class_="table-responsive")
    ]
    assert not bare, f"{url}: tables not wrapped in .table-responsive: {bare}"
