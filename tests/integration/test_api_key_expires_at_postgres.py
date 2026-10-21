"""``api_keys.expires_at`` as the migration chain builds it.

The SQLite suite builds the schema with ``create_all`` and never runs Alembic,
so keeping existing keys unexpiring, the column comments and the index are only
provable here, on a database the real chain migrated over existing rows.
"""
from datetime import datetime

import pytest
from sqlalchemy import text

from .conftest import flask_db

pytestmark = pytest.mark.postgres

_BEFORE = "5006b6594383"
_REVISION = "b1c2d3e4f5a6"
_CREATED = datetime(2026, 1, 1, 12, 0, 0)
_EXPIRES_AT_COMMENT = (
    "UTC time the key expires and is revoked (revoked_at set to this value); "
    "null if the key never expires"
)
_REVOKED_BY_COMMENT = (
    "Entity (user) that revoked this key; null while usable, for keys revoked before "
    "this column existed, for system revocations (OAuth code replay, key expiry), or if the revoker was deleted"
)
_OLD_REVOKED_BY_COMMENT = (
    "Entity (user) that revoked this key; null while usable, for keys revoked before "
    "this column existed, for system revocations (OAuth code replay), or if the revoker was deleted"
)


def _seed(engine):
    with engine.begin() as conn:
        entity_id = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'expires-at', 'EA', true) RETURNING id"
        )).scalar()
        for name, revoked_at in [("live", None), ("revoked", _CREATED)]:
            conn.execute(text(
                "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
                " output_tokens, audio_seconds, cost, created_at, revoked_at)"
                " VALUES (:e, :name, :name, 0, 0, 0, 0, 0, :created, :revoked_at)"
            ), {"e": entity_id, "name": name, "created": _CREATED, "revoked_at": revoked_at})


def _comments(conn):
    return dict(conn.execute(text("""
        SELECT attname, col_description('api_keys'::regclass, attnum)
        FROM pg_attribute
        WHERE attrelid = 'api_keys'::regclass AND attname IN ('expires_at', 'revoked_by_entity_id')
    """)).all())


def _has_index(conn):
    return conn.execute(text(
        "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'api_keys' AND indexname = 'ix_api_keys_expires_at'"
    )).scalar() == 1


def test_upgrade_keeps_existing_keys_unexpiring_and_downgrade_drops_column(pg_blank):
    url, engine = pg_blank
    flask_db(url, "upgrade", _BEFORE)
    _seed(engine)
    engine.dispose()

    flask_db(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        expires = dict(conn.execute(text("SELECT name, expires_at FROM api_keys")).all())
        assert _comments(conn) == {"expires_at": _EXPIRES_AT_COMMENT, "revoked_by_entity_id": _REVOKED_BY_COMMENT}
        assert _has_index(conn)
    assert expires == {"live": None, "revoked": None}
    engine.dispose()

    flask_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        columns = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'api_keys'"
        )).scalars())
        assert _comments(conn) == {"revoked_by_entity_id": _OLD_REVOKED_BY_COMMENT}
        assert not _has_index(conn)
    assert "expires_at" not in columns
