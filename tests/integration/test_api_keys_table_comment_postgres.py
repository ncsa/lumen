"""The ``api_keys`` table comment as the migration chain leaves it.

The SQLite suite has no table comments and never runs Alembic, so only a real
PostgreSQL can show that ``f9b1d3e5a7c9`` replaces the old "SHA-256 hash" text
with the model's HMAC-SHA256 comment, and that downgrade restores it. The
module steps its private database down one revision and back up, so it runs
against ``pg_migrated_isolated`` and leaves it at head.
"""

import pytest
from sqlalchemy import text

from tests.integration.conftest import migrate_db

pytestmark = pytest.mark.postgres

_BEFORE = "e8a0c2d4f6b8"
_REVISION = "f9b1d3e5a7c9"
_OLD = "API keys for entity authentication; only the SHA-256 hash is stored, never the plaintext"


def _table_comment(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT obj_description('api_keys'::regclass, 'pg_class')")).scalar()


def test_upgrade_sets_hmac_comment_and_downgrade_restores_old(pg_migrated_isolated):
    from lumen.models.api_key import APIKey

    url, engine = pg_migrated_isolated

    assert _table_comment(engine) == APIKey.__table__.comment

    migrate_db(url, "downgrade", _BEFORE)
    engine.dispose()
    assert _table_comment(engine) == _OLD
    engine.dispose()

    migrate_db(url, "upgrade", _REVISION)
    assert _table_comment(engine) == APIKey.__table__.comment
    engine.dispose()
