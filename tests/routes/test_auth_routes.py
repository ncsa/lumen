from http import HTTPStatus

from sqlalchemy import select


def test_landing_unauthenticated(client):
    resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    assert b"testuser" in resp.data or b"login" in resp.data.lower() or b"Login" in resp.data


def test_landing_authenticated_redirects_to_chat(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == HTTPStatus.FOUND
    assert "/chat" in resp.headers["Location"]


def test_devlogin_creates_user(app, client):
    resp = client.get("/devlogin", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/chat" in resp.headers["Location"]

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.execute(select(Entity).filter_by(email="testuser@example.com")).scalar_one_or_none()
        assert entity is not None
        assert entity.active is True


def test_devlogin_sets_session(client):
    with client.session_transaction() as sess:
        assert "entity_id" not in sess
    client.get("/devlogin")
    with client.session_transaction() as sess:
        assert "entity_id" in sess


def test_devlogin_reuses_existing_user(app, client):
    client.get("/devlogin")
    with client.session_transaction() as sess:
        first_id = sess["entity_id"]

    # Second login should reuse same entity
    client.get("/logout")
    client.get("/devlogin")
    with client.session_transaction() as sess:
        second_id = sess["entity_id"]

    assert first_id == second_id


def test_devlogin_assigns_dev_groups(app, client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.entity import Entity
        db.session.add(Group(name="dev-group", active=True, config_managed=True))
        db.session.commit()

    original = app.config.get("DEV_USER_GROUPS", [])
    app.config["DEV_USER_GROUPS"] = ["dev-group"]
    try:
        client.get("/devlogin")
    finally:
        app.config["DEV_USER_GROUPS"] = original

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        entity = db.session.execute(select(Entity).filter_by(email="testuser@example.com")).scalar_one_or_none()
        group = db.session.execute(select(Group).filter_by(name="dev-group")).scalar_one_or_none()
        member = db.session.execute(select(GroupMember).filter_by(entity_id=entity.id, group_id=group.id)).scalar_one_or_none()
        assert member is not None


def test_logout_clears_session(auth_client):
    with auth_client.session_transaction() as sess:
        assert "entity_id" in sess

    resp = auth_client.get("/logout", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND

    with auth_client.session_transaction() as sess:
        assert "entity_id" not in sess


def test_logout_redirects_to_landing(auth_client):
    resp = auth_client.get("/logout", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/" in resp.headers["Location"]


def test_devlogin_returns_403_when_not_configured(app, client):
    """devlogin must return 403 when DEV_USER is not set in config."""
    original = app.config.pop("DEV_USER", None)
    try:
        resp = client.get("/devlogin")
        assert resp.status_code == HTTPStatus.FORBIDDEN
    finally:
        if original is not None:
            app.config["DEV_USER"] = original


# ---------------------------------------------------------------------------
# OAuth callback — email_verified gate
# ---------------------------------------------------------------------------

def _callback_with_userinfo(client, userinfo):
    from unittest.mock import MagicMock, patch
    from lumen.blueprints.auth import routes as auth_routes
    provider = MagicMock()
    provider.authorize_access_token.return_value = {"userinfo": userinfo}
    with patch.object(auth_routes.oauth, "provider", provider, create=True):
        return client.get("/callback", follow_redirects=False)


def test_callback_rejects_explicitly_unverified_email(client):
    resp = _callback_with_userinfo(client, {"email": "unv@example.com", "email_verified": False, "name": "U"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_callback_allows_unverified_email_when_configured(app, client):
    app.config["OAUTH2_ALLOW_UNVERIFIED_EMAIL"] = True
    try:
        resp = _callback_with_userinfo(client, {"email": "unv2@example.com", "email_verified": False, "name": "U"})
        assert resp.status_code == HTTPStatus.FOUND
        assert "/chat" in resp.headers["Location"]
    finally:
        app.config["OAUTH2_ALLOW_UNVERIFIED_EMAIL"] = False


def test_callback_allows_missing_email_verified_claim(client):
    resp = _callback_with_userinfo(client, {"email": "miss@example.com", "name": "U"})
    assert resp.status_code == HTTPStatus.FOUND
    assert "/chat" in resp.headers["Location"]


def test_callback_allows_verified_email(client):
    resp = _callback_with_userinfo(client, {"email": "ver@example.com", "email_verified": True, "name": "U"})
    assert resp.status_code == HTTPStatus.FOUND
    assert "/chat" in resp.headers["Location"]
