from http import HTTPStatus


def test_models_requires_login(client):
    resp = client.get("/models", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/" in resp.headers["Location"]


def test_models_lists_active_model(app, auth_client, test_model):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_models_blocked_model_not_listed(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blacklist",
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_models_group_blacklisted_not_listed(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=test_user["id"], group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=test_model["id"], access_type="blacklist"))
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_models_inactive_model_not_listed(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-model", input_cost_per_million=1.0, output_cost_per_million=1.0, active=False)
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


def test_model_detail_blocked_returns_404(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blacklist",
        ))
        db.session.commit()

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
