"""The ``api_keys`` table comment as the migration chain leaves it.

The SQLite suite has no table comments and never runs Alembic, so only a real
PostgreSQL can show that ``f9b1d3e5a7c9`` replaces the old "SHA-256 hash" text
with the model's HMAC-SHA256 comment, and that downgrade restores it. The
module steps its private database down one revision and back up, so it runs
against ``pg_migrated_isolated`` and leaves it at head.

The same revision, being one statement, also covers ``migrate_db``'s offline
mode (``flask db upgrade|downgrade <range> --sql``): the SQL comes back as the
return value, nothing reaches the real stdout, and the database is untouched.
"""
import sys

import pytest
from sqlalchemy import text

from tests.integration.conftest import migrate_db
from tests.unit.test_migrations import _script_dir

pytestmark = pytest.mark.postgres

_BEFORE = "e8a0c2d4f6b8"
_REVISION = "f9b1d3e5a7c9"
_OLD = "API keys for entity authentication; only the SHA-256 hash is stored, never the plaintext"


def _version(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _assert_offline(engine, capsys, stdout, sql, comment, version):
    """``sql`` sets ``comment`` and stamps ``version``; stdout is back and saw none of it."""
    assert sys.stdout is stdout
    assert capsys.readouterr().out == ""
    assert f"COMMENT ON TABLE api_keys IS '{comment}'" in sql
    assert f"UPDATE alembic_version SET version_num='{version}'" in sql
    # Offline: nothing ran against the database, which stays at head.
    assert _version(engine) == _script_dir().get_current_head()


def test_offline_upgrade_returns_the_sql(pg_migrated_isolated, capsys):
    from lumen.models.api_key import APIKey

    url, engine = pg_migrated_isolated
    stdout = sys.stdout
    sql = migrate_db(url, "upgrade", f"{_BEFORE}:{_REVISION}", sql=True)
    _assert_offline(engine, capsys, stdout, sql, APIKey.__table__.comment, _REVISION)
    engine.dispose()


def test_offline_downgrade_returns_the_sql(pg_migrated_isolated, capsys):
    url, engine = pg_migrated_isolated
    stdout = sys.stdout
    sql = migrate_db(url, "downgrade", f"{_REVISION}:{_BEFORE}", sql=True)
    _assert_offline(engine, capsys, stdout, sql, _OLD, _BEFORE)
    engine.dispose()


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
