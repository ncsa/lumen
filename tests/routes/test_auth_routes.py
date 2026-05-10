from sqlalchemy import select


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
    assert resp.status_code == 302

    with auth_client.session_transaction() as sess:
        assert "entity_id" not in sess


def test_logout_redirects_to_landing(auth_client):
    resp = auth_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/" in resp.headers["Location"]


def test_devlogin_returns_403_when_not_configured(app, client):
    """devlogin must return 403 when DEV_USER is not set in config."""
    original = app.config.pop("DEV_USER", None)
    try:
        resp = client.get("/devlogin")
        assert resp.status_code == 403
    finally:
        if original is not None:
            app.config["DEV_USER"] = original
