"""The 'ghost' entity type as the migration chain builds it on PostgreSQL.

ck_entities_type and the column comments exist only on PostgreSQL (the SQLite
suite builds the schema with ``create_all``), so only a real database can show
that b4c6d8e0f2a4 widens the CHECK, adds the single-ghost index, refreshes the
comments, and that its downgrade restores them and refuses while a ghost row
exists. The module steps its private database below the revision and back up,
so it runs against ``pg_migrated_isolated`` and leaves it at head.
"""
import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

_BEFORE = "d3e4f5a6b7c8"
_REVISION = "b4c6d8e0f2a4"
_NEW_COMMENTS = {
    ("entities", "entity_type"):
        "'user' for OAuth users, 'project' for API projects, 'ghost' for the single shared erased-user entity",
    ("api_keys", "key_hash"): "HMAC-SHA256 of the raw key, keyed on ENCRYPTION_KEY",
}
_OLD_COMMENTS = {
    ("entities", "entity_type"): "'user' for OAuth users, 'project' for API projects",
    ("api_keys", "key_hash"): "SHA-256 hash of the raw key; null only during legacy migration",
}
_INSERT_GHOST = text(
    "INSERT INTO entities (entity_type, name, initials, active, store_conversations)"
    " VALUES ('ghost', 'Erased user', '', false, false)"
)


def _flask_db(url, *args):
    return subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"},
    )


def _ok(result, what):
    assert result.returncode == 0, (
        f"flask db {what} failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def _comments(engine):
    with engine.connect() as conn:
        return {
            (table, column): conn.execute(text(
                "SELECT col_description(CAST(:t AS regclass), attnum) FROM pg_attribute"
                " WHERE attrelid = CAST(:t AS regclass) AND attname = :c"
            ), {"t": table, "c": column}).scalar()
            for table, column in _NEW_COMMENTS
        }


def _has_ghost_index(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_entities_single_ghost'"
        )).scalar()


def test_head_allows_exactly_one_ghost_and_downgrade_restores_two_types(pg_migrated_isolated):
    from lumen.models.api_key import APIKey
    from lumen.models.entity import Entity

    url, engine = pg_migrated_isolated

    # At head: the comments match the models, and one ghost fits but not two.
    assert _comments(engine) == _NEW_COMMENTS
    assert _NEW_COMMENTS[("entities", "entity_type")] == Entity.__table__.c.entity_type.comment
    assert _NEW_COMMENTS[("api_keys", "key_hash")] == APIKey.__table__.c.key_hash.comment
    indexdef = _has_ghost_index(engine)
    assert "UNIQUE" in indexdef and "WHERE" in indexdef and "ghost" in indexdef
    with engine.begin() as conn:
        conn.execute(_INSERT_GHOST)
    with pytest.raises(IntegrityError, match="uq_entities_single_ghost"):
        with engine.begin() as conn:
            conn.execute(_INSERT_GHOST)

    # The downgrade refuses while the ghost exists.
    engine.dispose()
    result = _flask_db(url, "downgrade", _BEFORE)
    assert result.returncode != 0
    assert "ck_entities_type" in result.stdout + result.stderr

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM entities WHERE entity_type = 'ghost'"))
    engine.dispose()
    _ok(_flask_db(url, "downgrade", _BEFORE), "downgrade")
    assert _comments(engine) == _OLD_COMMENTS
    assert _has_ghost_index(engine) is None
    with pytest.raises(IntegrityError, match="ck_entities_type"):
        with engine.begin() as conn:
            conn.execute(_INSERT_GHOST)
    engine.dispose()

    _ok(_flask_db(url, "upgrade", _REVISION), "upgrade")
    assert _comments(engine) == _NEW_COMMENTS
    assert _has_ghost_index(engine)
    engine.dispose()
