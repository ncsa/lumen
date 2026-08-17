import os
from pathlib import Path

import pytest

TEST_CONFIG = str(Path(__file__).parent / "fixtures" / "test_config.yaml")
DB_PATH = Path(__file__).parent.parent / "test_lumen.db"


@pytest.fixture(scope="session")
def app():
    os.environ.update({
        "CONFIG_YAML": TEST_CONFIG,
        "BACKGROUND_WORKER": "false",
    })
    from lumen import create_app
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        from lumen.extensions import db
        db.create_all()
    yield application
    with application.app_context():
        from lumen.extensions import db
        db.drop_all()
    DB_PATH.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def clean_db(app):
    yield
    with app.app_context():
        from lumen.extensions import db
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def test_user(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = Entity(
            entity_type="user",
            email="testuser@example.com",
            name="Test User",
            initials="TU",
            gravatar_hash="abc123",
            active=True,
        )
        db.session.add(entity)
        db.session.commit()
        db.session.refresh(entity)
        # Capture scalar values before context closes
        return {"id": entity.id, "name": entity.name, "initials": entity.initials, "gravatar_hash": entity.gravatar_hash or ""}


@pytest.fixture
def auth_client(client, test_user):
    with client.session_transaction() as sess:
        sess["entity_id"] = test_user["id"]
        sess["entity_name"] = test_user["name"]
        sess["initials"] = test_user["initials"]
        sess["gravatar_hash"] = test_user["gravatar_hash"]
    return client


@pytest.fixture
def test_model(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(
            model_name="test-model",
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
        )
        db.session.add(m)
        db.session.commit()
        db.session.refresh(m)
        return {"id": m.id, "model_name": m.model_name}


def set_model_owner(model_id, owner_entity_id):
    """Set (or clear) a model's owner. Call inside an app context; commits."""
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    db.session.get(ModelConfig, model_id).owner_entity_id = owner_entity_id
    db.session.commit()


def grant_model_to_group(model_id, group_id):
    """Grant an owned model to a group. Call inside an app context; commits."""
    from lumen.extensions import db
    from lumen.models.model_group_access import ModelGroupAccess
    db.session.add(ModelGroupAccess(model_config_id=model_id, group_id=group_id))
    db.session.commit()


def make_group_with_member(entity_id, name="test-group", active=True):
    """Create a group containing entity_id; returns the group id. Call inside an app context; commits."""
    from lumen.extensions import db
    from lumen.models.group import Group
    from lumen.models.group_member import GroupMember
    g = Group(name=name, active=active)
    db.session.add(g)
    db.session.flush()
    db.session.add(GroupMember(entity_id=entity_id, group_id=g.id))
    db.session.commit()
    return g.id


@pytest.fixture
def admin_user(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = Entity(
            entity_type="user",
            email="admin@example.com",
            name="Admin User",
            initials="AU",
            gravatar_hash="def456",
            active=True,
        )
        db.session.add(entity)
        db.session.commit()
        db.session.refresh(entity)
        return {"id": entity.id, "name": entity.name, "initials": entity.initials, "gravatar_hash": entity.gravatar_hash or ""}


@pytest.fixture
def admin_client(client, admin_user):
    with client.session_transaction() as sess:
        sess["entity_id"] = admin_user["id"]
        sess["entity_name"] = admin_user["name"]
        sess["initials"] = admin_user["initials"]
        sess["gravatar_hash"] = admin_user["gravatar_hash"]
        sess["admin_mode"] = True
    return client


@pytest.fixture
def admin_client_no_mode(client, admin_user):
    """Admin-eligible user with admin mode off (the default after login)."""
    with client.session_transaction() as sess:
        sess["entity_id"] = admin_user["id"]
        sess["entity_name"] = admin_user["name"]
        sess["initials"] = admin_user["initials"]
        sess["gravatar_hash"] = admin_user["gravatar_hash"]
    return client


@pytest.fixture
def test_model_endpoint(app, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        ep = ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="test-api-key-123",
            model_name="dummy",
            healthy=True,
        )
        db.session.add(ep)
        db.session.commit()
        db.session.refresh(ep)
        return {"id": ep.id, "model_config_id": ep.model_config_id, "healthy": ep.healthy}
