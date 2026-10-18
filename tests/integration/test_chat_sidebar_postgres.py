"""The chat sidebar's latest-message preview query on PostgreSQL.

Runs the same scenarios as ``tests/routes/test_chat_routes.py``: ``substr``,
the ``max()`` join and the ``created_at`` tie-break on ``id`` all have to
behave identically on both backends.
"""

import os
import uuid

import pytest

from tests.integration.conftest import TEST_CONFIG
from tests.routes.test_chat_routes import (
    sidebar_created_at_tie_takes_highest_id,
    sidebar_empty_conversation,
    sidebar_ignores_conversations_outside_the_page,
    sidebar_newest_message,
    sidebar_truncates_to_60_characters,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A Flask app bound to this module's migrated PostgreSQL database.

    ``config.Config`` reads ``DATABASE_URL`` at class-definition time and the root
    conftest's SQLite app has already imported it, so the attribute is patched too.
    """
    url = pg_migrated_isolated[0]
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    from config import Config
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app
    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        from lumen.extensions import db
        assert db.engine.dialect.name == "postgresql"
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


@pytest.fixture
def pg_user_client(pg_app):
    """A fresh user per test, so each scenario sees only its own conversations."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    with pg_app.app_context():
        tag = uuid.uuid4().hex[:8]
        entity = Entity(entity_type="user", email=f"sidebar-{tag}@example.com",
                        name=f"Sidebar {tag}", initials="SB", active=True)
        db.session.add(entity)
        db.session.commit()
        eid = entity.id
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = eid
        sess["entity_name"] = f"Sidebar {tag}"
        sess["initials"] = "SB"
        sess["gravatar_hash"] = ""
    return pg_app, client, eid


@pytest.mark.parametrize("scenario", [
    sidebar_newest_message,
    sidebar_truncates_to_60_characters,
    sidebar_empty_conversation,
    sidebar_created_at_tie_takes_highest_id,
    sidebar_ignores_conversations_outside_the_page,
], ids=lambda f: f.__name__)
def test_sidebar_preview_on_postgres(pg_user_client, scenario):
    scenario(*pg_user_client)
