"""Basic smoke tests for chat, usage, services, and admin pages."""


def test_chat_page_requires_login(client):
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == 302


def test_chat_page_loads(auth_client):
    resp = auth_client.get("/chat")
    assert resp.status_code == 200
    assert b"chat" in resp.data.lower()


def test_chat_page_with_model(app, auth_client, test_model):
    """Verifies chat page renders with an available model + healthy endpoint."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="k",
            healthy=True,
        ))
        from lumen.models.entity import Entity
        entity = Entity.query.filter_by(email="testuser@example.com").first()
        if entity:
            db.session.add(EntityLimit(entity_id=entity.id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()

    resp = auth_client.get("/chat")
    assert resp.status_code == 200


def test_usage_page_requires_login(client):
    resp = client.get("/usage", follow_redirects=False)
    assert resp.status_code == 302


def test_usage_page_loads(auth_client):
    resp = auth_client.get("/usage")
    assert resp.status_code == 200


def test_services_page_requires_login(client):
    resp = client.get("/services", follow_redirects=False)
    assert resp.status_code == 302


def test_services_page_loads(auth_client):
    resp = auth_client.get("/services")
    assert resp.status_code == 200


def test_admin_groups_requires_auth(client):
    resp = client.get("/admin/groups", follow_redirects=False)
    assert resp.status_code == 302


def test_admin_groups_forbidden_for_non_admin(auth_client):
    resp = auth_client.get("/admin/groups")
    assert resp.status_code == 403


def test_admin_groups_loads_for_admin(admin_client):
    resp = admin_client.get("/admin/groups")
    assert resp.status_code == 200


def test_admin_users_loads_for_admin(admin_client):
    resp = admin_client.get("/admin/users")
    assert resp.status_code == 200


def test_admin_analytics_loads_for_admin(admin_client):
    resp = admin_client.get("/admin/analytics")
    assert resp.status_code == 200
