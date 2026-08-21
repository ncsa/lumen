import os
from pathlib import Path

import pytest

TEST_CONFIG = str(Path(__file__).parent / "fixtures" / "test_config.yaml")


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    # A database file unique to this pytest process. The teardown below drops
    # every table and deletes the file, so two pytest runs sharing one path
    # destroy each other: the second run's tests fail mid-flight with
    # "no such table: request_logs" from the autouse clean_db fixture, which
    # reads as a bug in whatever was being tested. That is not hypothetical --
    # it showed up while two agents ran different test files in this tree at the
    # same time, and it would show up again under pytest-xdist or two terminals.
    # DATABASE_URL wins over the yaml url (see config.py and create_app), so
    # setting it here is enough to isolate the run.
    db_path = tmp_path_factory.mktemp("db") / "test_lumen.db"
    os.environ.update({
        "CONFIG_YAML": TEST_CONFIG,
        "BACKGROUND_WORKER": "false",
        "DATABASE_URL": f"sqlite:///{db_path}",
    })
    from lumen import create_app
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        from lumen.extensions import db
        # Ask the engine where the file actually is instead of assuming the repo
        # root: Flask-SQLAlchemy resolves a relative sqlite path against
        # app.instance_path. Then refuse to run against the dev database — this
        # suite drops every table at teardown and deletes every row between
        # tests, so a misrouted URI destroys real data silently. That is not
        # hypothetical: `app.database_url` in the test config went unread for six
        # weeks after the key moved to `app.database.url`, and the whole suite ran
        # against instance/lumen_dev.db the entire time.
        db_file = db.engine.url.database
        assert (
            db.engine.url.get_backend_name() == "sqlite"
            and db_file
            and Path(db_file).name == "test_lumen.db"
        ), (
            f"tests are pointed at {db_file!r} (backend "
            f"{db.engine.url.get_backend_name()}), which is not the dedicated test "
            "database; this suite drops every table at teardown and deletes every "
            "row between tests, so a misrouted URI destroys real data silently"
        )
        db.create_all()
    yield application
    with application.app_context():
        from lumen.extensions import db
        db.drop_all()
        backend = db.engine.url.get_backend_name()
    if backend == "sqlite":
        Path(db_file).unlink(missing_ok=True)


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
            access="allowed",
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
