from http import HTTPStatus


def test_admin_required_returns_403_for_non_admin(app, auth_client, test_user):
    """Non-admin user should get 403 from admin endpoints."""
    resp = auth_client.get("/admin/users")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_admin_required_redirects_unauthenticated(client):
    """Unauthenticated user is redirected rather than getting 403."""
    resp = client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_login_required_redirects_unauthenticated(client):
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/" in resp.headers["Location"]


def test_login_required_allows_authenticated(auth_client):
    resp = auth_client.get("/chat", follow_redirects=False)
    # Should reach the chat page (200) or redirect within the app — not to landing
    assert resp.status_code in (HTTPStatus.OK, HTTPStatus.FOUND)
    if resp.status_code == HTTPStatus.FOUND:
        assert "/chat" in resp.headers["Location"] or "chat" in resp.headers["Location"]


def test_inactive_user_redirected(app, client):
    """An inactive user should be redirected even with a valid session."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = Entity(
            entity_type="user", email="inactive@example.com",
            name="Inactive", initials="IN", active=False,
        )
        db.session.add(entity)
        db.session.commit()
        entity_id = entity.id

    with client.session_transaction() as sess:
        sess["entity_id"] = entity_id

    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_admin_required_rejects_inactive_admin(app, admin_client, admin_user):
    """A deactivated admin with admin mode still on is logged out of admin pages.

    Deactivation must take effect immediately: the session (including
    admin_mode) is cleared and the request is redirected to the landing page,
    as login_required does for inactive users.
    """
    from urllib.parse import urlparse

    from lumen.extensions import db
    from lumen.models.entity import Entity

    assert admin_client.get("/admin/users").status_code == HTTPStatus.OK

    with app.app_context():
        entity = db.session.get(Entity, admin_user["id"])
        entity.active = False
        db.session.commit()

    resp = admin_client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert urlparse(resp.headers["Location"]).path == "/"

    with admin_client.session_transaction() as sess:
        assert "entity_id" not in sess
        assert "admin_mode" not in sess
