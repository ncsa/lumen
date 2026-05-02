def test_admin_required_returns_403_for_non_admin(app, auth_client, test_user):
    """Non-admin user should get 403 from admin endpoints."""
    resp = auth_client.get("/admin/groups")
    assert resp.status_code == 403


def test_admin_required_redirects_unauthenticated(client):
    """Unauthenticated user is redirected rather than getting 403."""
    resp = client.get("/admin/groups", follow_redirects=False)
    assert resp.status_code == 302


def test_login_required_redirects_unauthenticated(client):
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == 302
    assert "/" in resp.headers["Location"]


def test_login_required_allows_authenticated(auth_client):
    resp = auth_client.get("/chat", follow_redirects=False)
    # Should reach the chat page (200) or redirect within the app — not to landing
    assert resp.status_code in (200, 302)
    if resp.status_code == 302:
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
    assert resp.status_code == 302
