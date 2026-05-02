def test_landing_unauthenticated(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"testuser" in resp.data or b"login" in resp.data.lower() or b"Login" in resp.data


def test_landing_authenticated_redirects_to_chat(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 302
    assert "/chat" in resp.headers["Location"]


def test_devlogin_creates_user(app, client):
    resp = client.get("/devlogin", follow_redirects=False)
    assert resp.status_code == 302
    assert "/chat" in resp.headers["Location"]

    with app.app_context():
        from lumen.models.entity import Entity
        entity = Entity.query.filter_by(email="testuser@example.com").first()
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


def test_logout_clears_session(auth_client):
    with auth_client.session_transaction() as sess:
        assert "entity_id" in sess

    resp = auth_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302

    with auth_client.session_transaction() as sess:
        assert "entity_id" not in sess


def test_logout_redirects_to_landing(auth_client):
    resp = auth_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/" in resp.headers["Location"]
