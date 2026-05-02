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
            active=True,
        )
        db.session.add(m)
        db.session.commit()
        db.session.refresh(m)
        return {"id": m.id, "model_name": m.model_name}


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
