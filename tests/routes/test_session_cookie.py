"""The session cookie carries the user's public UUID, never an integer id."""

from http import HTTPStatus
from urllib.parse import urlparse
from uuid import UUID

from flask import g, session

from lumen.decorators import load_logged_in_entity
from lumen.extensions import _chat_entity_id, db
from lumen.models.entity import Entity
from tests.conftest import make_project


def _decoded_cookie(app, client):
    """The session cookie as the browser holds it, decoded (it is signed, not encrypted)."""
    cookie = client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
    assert cookie is not None
    return app.session_interface.get_signing_serializer(app).loads(cookie.value)


def _integers(value, path=""):
    """Every int (not bool) anywhere in a decoded session, with its key path."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _integers(v, f"{path}.{k}" if path else k)]
    if isinstance(value, (list, tuple)):
        return [p for i, v in enumerate(value) for p in _integers(v, f"{path}[{i}]")]
    return []


def test_old_integer_cookie_is_logged_out(client, test_user):
    """A cookie written before the switch (integer entity_id, no entity_uuid) counts as logged out."""
    with client.session_transaction() as sess:
        sess["entity_id"] = test_user["id"]
        sess["entity_name"] = test_user["name"]
        sess["admin_mode"] = True
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert urlparse(resp.headers["Location"]).path == "/"
    with client.session_transaction() as sess:
        assert "entity_id" not in sess
        assert "admin_mode" not in sess
    # The landing page shows the login screen instead of bouncing back to /chat.
    assert client.get("/", follow_redirects=False).status_code == HTTPStatus.OK


def test_old_integer_cookie_cannot_reach_json_endpoints(client, test_user):
    with client.session_transaction() as sess:
        sess["entity_id"] = test_user["id"]
    resp = client.get("/chat/conversations", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_unknown_or_malformed_uuid_is_logged_out(client):
    for value in ("not-a-uuid", "00000000-0000-0000-0000-000000000000", "1"):
        with client.session_transaction() as sess:
            sess["entity_uuid"] = value
        resp = client.get("/chat", follow_redirects=False)
        assert resp.status_code == HTTPStatus.FOUND
        assert urlparse(resp.headers["Location"]).path == "/"


def test_project_uuid_in_cookie_is_not_a_login(client, app, test_user):
    """Only users log in: a project's uuid in the cookie resolves to nobody."""
    with app.app_context():
        project_uuid = str(make_project("Lab", owner_id=test_user["id"]).uuid)
    with client.session_transaction() as sess:
        sess["entity_uuid"] = project_uuid
    assert client.get("/chat", follow_redirects=False).status_code == HTTPStatus.FOUND


def test_cookie_after_login_holds_uuid_and_no_integer(client, app, test_user):
    with app.app_context():
        project_uuid = str(make_project("Lab", owner_id=test_user["id"]).uuid)

    assert client.get("/devlogin", follow_redirects=False).status_code == HTTPStatus.FOUND
    cookie = _decoded_cookie(app, client)
    assert cookie["entity_uuid"] == test_user["uuid"]
    assert "entity_id" not in cookie
    assert _integers(cookie) == []

    # A rendered page caches the nav (admin flag, managed projects) in the cookie.
    assert client.get("/chat").status_code == HTTPStatus.OK
    cookie = _decoded_cookie(app, client)
    assert cookie["_nav"]["project_ids"] == [project_uuid]
    # The cache version is the only integer left: it is a schema number, not an id.
    assert _integers(cookie) == ["_nav.v"]
    for value in cookie["_nav"]["project_ids"]:
        UUID(value)


def test_stale_nav_cache_is_rebuilt_with_uuids(auth_client, app, test_user):
    """A v2 nav cache from before the switch holds integer project ids; it is replaced."""
    with app.app_context():
        project = make_project("Lab", owner_id=test_user["id"])
        project_id, project_uuid = project.id, str(project.uuid)
    with auth_client.session_transaction() as sess:
        sess["_nav"] = {"v": 2, "is_admin": False, "project_ids": [project_id]}
    assert auth_client.get("/chat").status_code == HTTPStatus.OK
    with auth_client.session_transaction() as sess:
        assert sess["_nav"] == {"v": 3, "is_admin": False, "project_ids": [project_uuid]}


def test_request_resolves_the_logged_in_entity(app, test_user):
    with app.test_request_context("/chat"):
        session["entity_uuid"] = test_user["uuid"]
        load_logged_in_entity()
        assert g.entity_id == test_user["id"]
        assert g.entity.email == "testuser@example.com"


def test_rate_limit_key_is_the_uuid(app, test_user):
    with app.test_request_context("/chat", environ_base={"REMOTE_ADDR": "10.1.2.3"}):
        g.entity = None
        assert _chat_entity_id() == "10.1.2.3"
        g.entity = db.session.get(Entity, test_user["id"])
        assert _chat_entity_id() == test_user["uuid"]
