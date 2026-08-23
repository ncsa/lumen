from datetime import timedelta
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


def test_model_detail_blocked_returns_not_found(app, auth_client, test_model, test_user):
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert test_model["model_name"].encode() not in resp.data


def test_model_detail_inactive_returns_not_found(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-detail", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/inactive-detail")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert b"inactive-detail" not in resp.data


def test_model_detail_expired_returns_not_found(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.timeutils import utcnow

        m = ModelConfig(
            model_name="expired-detail",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            end_date=utcnow() - timedelta(seconds=1),
        )
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/expired-detail")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert b"expired-detail" not in resp.data


def test_model_readme_inactive_returns_not_found_without_fetching(app, auth_client, monkeypatch):
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

    from lumen.blueprints.models_page import routes

    def fail_fetch(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(routes.http_requests, "get", fail_fetch)

    resp = auth_client.get("/models/inactive-readme/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_readme_blocked_returns_not_found_without_fetching(
    app, auth_client, test_model, test_user, monkeypatch
):
    from tests.conftest import set_model_owner

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        model = db.session.get(ModelConfig, test_model["id"])
        model.url = "https://huggingface.co/org/private-model"
        db.session.commit()
        set_model_owner(test_model["id"], _make_other_owner(app))

    from lumen.blueprints.models_page import routes

    def fail_fetch(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(routes.http_requests, "get", fail_fetch)

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_detail_needs_ack_remains_visible(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data
