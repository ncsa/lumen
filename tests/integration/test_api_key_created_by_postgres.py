"""``api_keys.created_by_entity_id`` as the migration chain actually builds it.

The SQLite suite builds the schema with ``create_all`` and never runs Alembic,
so the migration's own PostgreSQL path — the FK clause and the column comment —
is only provable here, on a database the real chain migrated.
"""
import os

import pytest
from sqlalchemy import delete, select, text

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A real Flask app bound to this module's migrated database.

    The ORM models matter here: the point is that the migrated table satisfies
    the same contract (nullable creator FK, SET NULL) the models declare.
    """
    url = pg_migrated_isolated[0]
    previous_env = {k: os.environ.get(k) for k in ("DATABASE_URL", "CONFIG_YAML", "BACKGROUND_WORKER")}
    os.environ.update({"DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"})
    from config import Config

    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app

    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        from lumen.extensions import db

        assert db.engine.dialect.name == "postgresql", "the test app is not on PostgreSQL"
    yield application
    for key, value in previous_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_migrated_creator_fk_is_set_null_and_commented(pg_app):
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.entity import Entity
    from lumen.services.crypto import hash_api_key

    with pg_app.app_context():
        fk = db.session.execute(text("""
            SELECT con.confdeltype FROM pg_constraint con
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
            WHERE con.contype = 'f' AND con.conrelid = 'api_keys'::regclass
              AND att.attname = 'created_by_entity_id'
        """)).scalar()
        assert fk == "n", f"created_by_entity_id FK uses ON DELETE type {fk!r}, expected 'n' (SET NULL)"

        comment = db.session.execute(text("""
            SELECT col_description('api_keys'::regclass, attnum)
            FROM pg_attribute
            WHERE attrelid = 'api_keys'::regclass AND attname = 'created_by_entity_id'
        """)).scalar()
        assert comment == APIKey.__table__.columns["created_by_entity_id"].comment

        creator = Entity(entity_type="user", email="creator@example.invalid",
                         name="Key Creator", initials="KC", active=True)
        project = Entity(entity_type="project", name="set-null-proj", initials="SN", active=True)
        db.session.add_all([creator, project])
        db.session.flush()
        key = APIKey(entity_id=project.id, created_by_entity_id=creator.id,
                     name="k", key_hash=hash_api_key("sk_pgsetnull12345678"), active=True)
        db.session.add(key)
        db.session.commit()
        key_id, creator_id, project_id = key.id, creator.id, project.id

        db.session.execute(delete(Entity).where(Entity.id == creator_id))
        db.session.commit()
        db.session.expire_all()

        key = db.session.execute(select(APIKey).filter_by(id=key_id)).scalar_one()
        assert key.created_by_entity_id is None, "creator deletion did not SET NULL the reference"

        db.session.execute(delete(Entity).where(Entity.id == project_id))
        db.session.commit()
