"""The admin API user list's keyset paging on PostgreSQL.

Runs ``list_pages_through_every_sort`` from ``tests/routes/test_admin_api.py``:
the coalesced sort keys, the ``(sort value, entity id)`` cursor and the ties on
``created_at`` and ``last_active`` have to page identically on both backends.
"""

import os
import uuid

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_ADMIN, APIKey
from lumen.models.entity import Entity
from lumen.services.crypto import hash_api_key
from tests.conftest import no_snapshot_refresher
from tests.integration.conftest import TEST_CONFIG
from tests.routes.test_admin_api import list_pages_through_every_sort

pytestmark = pytest.mark.postgres

ADMIN_EMAIL = "admin@example.com"  # in the test config's admins list


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
    with pytest.MonkeyPatch.context() as mp:
        no_snapshot_refresher(mp)
        application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        assert db.engine.dialect.name == "postgresql"
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


@pytest.fixture
def admin_token(pg_app):
    """A fresh admin key owned by the configured admin."""
    token = f"sk_admin_pg_list_{uuid.uuid4().hex}"
    with pg_app.app_context():
        admin = db.session.execute(select(Entity).where(Entity.email == ADMIN_EMAIL)).scalar_one_or_none()
        if admin is None:
            admin = Entity(entity_type="user", email=ADMIN_EMAIL, name="Admin User", active=True)
            db.session.add(admin)
            db.session.flush()
        db.session.add(APIKey(entity_id=admin.id, name="pg list", key_type=KEY_TYPE_ADMIN,
                              key_hash=hash_api_key(token)))
        db.session.commit()
    return token


def test_list_pages_through_every_sort_on_postgres(pg_app, admin_token):
    list_pages_through_every_sort(pg_app, pg_app.test_client(), admin_token)
