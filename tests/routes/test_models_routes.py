from http import HTTPStatus


def test_models_requires_login(client):
    resp = client.get("/models", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/" in resp.headers["Location"]


def test_models_lists_active_model(app, auth_client, test_model):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def _make_other_owner(app):
    """Create a second user entity to own a model."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        return owner.id


def test_models_owned_model_not_listed(app, auth_client, test_model, test_user):
    """A model owned by someone else (no grant) is hidden from the list."""
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_models_inactive_granted_group_not_listed(app, auth_client, test_model, test_user):
    """A grant through an inactive group does not make an owned model visible."""
    from tests.conftest import grant_model_to_group, make_group_with_member, set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)
        group_id = make_group_with_member(test_user["id"], active=False)
        grant_model_to_group(test_model["id"], group_id)

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_models_inactive_model_not_listed(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-model", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert b"inactive-model" not in resp.data


def test_model_detail_requires_login(client, test_model):
    resp = client.get(f"/models/{test_model['model_name']}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_model_detail_404_unknown(auth_client):
    resp = auth_client.get("/models/does-not-exist")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_detail_ok(auth_client, test_model):
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_model_detail_blocked_renders_access_denied(app, auth_client, test_model, test_user):
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert b"Access denied" in resp.data


def test_model_detail_inactive_renders(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-detail", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/inactive-detail")
    assert resp.status_code == HTTPStatus.OK
    assert b"inactive-detail" in resp.data


def test_model_readme_inactive_serves_card(app, auth_client, monkeypatch):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(
            model_name="inactive-readme",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            url="https://huggingface.co/org/Some-Model",
            disabled=True,
        )
        db.session.add(m)
        db.session.commit()

    class FakeResponse:
        text = "---\nlicense: apache-2.0\n---\n# Card body\n"

        def raise_for_status(self):
            pass

    from lumen.blueprints.models_page import routes
    monkeypatch.setattr(routes.http_requests, "get", lambda *a, **kw: FakeResponse())

    resp = auth_client.get("/models/inactive-readme/readme")
    assert resp.status_code == HTTPStatus.OK
    assert resp.data == b"# Card body\n"
