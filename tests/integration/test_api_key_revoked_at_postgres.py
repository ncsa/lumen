"""``api_keys.revoked_at`` and ``revoked_by_entity_id`` as the migration chain builds them.

The SQLite suite builds the schema with ``create_all`` and never runs Alembic,
so the backfill of keys that were already inactive, the column comment, and the
downgrade back to ``active`` are only provable here, on a database the real
chain migrated over existing rows.
"""
from datetime import datetime

import pytest
from sqlalchemy import text

from .conftest import flask_db

pytestmark = pytest.mark.postgres

_BEFORE = "f8a9b0c1d2e3"
_REVISION = "a9b0c1d2e3f4"
_CREATED = datetime(2026, 1, 1, 12, 0, 0)
_USED = datetime(2026, 2, 1, 12, 0, 0)
_REVOKED_AT_COMMENT = (
    "UTC time the key was revoked; null while the key is usable; "
    "approximate for keys revoked before this column existed"
)
_REVOKED_BY_COMMENT = (
    "Entity (user) that revoked this key; null while usable, for keys revoked before "
    "this column existed, for system revocations (OAuth code replay), or if the revoker was deleted"
)


def _seed(engine):
    with engine.begin() as conn:
        entity_id = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'revoked-at', 'RA', true) RETURNING id"
        )).scalar()
        for name, active, last_used in [
            ("live", True, _USED),
            ("used_then_revoked", False, _USED),
            ("never_used_revoked", False, None),
        ]:
            conn.execute(text(
                "INSERT INTO api_keys (entity_id, name, key_hash, active, requests, input_tokens,"
                " output_tokens, audio_seconds, cost, last_used_at, created_at)"
                " VALUES (:e, :name, :name, :active, 0, 0, 0, 0, 0, :last_used, :created)"
            ), {"e": entity_id, "name": name, "active": active, "last_used": last_used,
                "created": _CREATED})


def _columns(engine):
    with engine.connect() as conn:
        return set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'api_keys'"
        )).scalars())


def test_upgrade_backfills_inactive_keys_and_downgrade_restores_active(pg_blank):
    url, engine = pg_blank
    flask_db(url, "upgrade", _BEFORE)
    _seed(engine)
    engine.dispose()

    flask_db(url, "upgrade", _REVISION)
    assert "active" not in _columns(engine)
    with engine.connect() as conn:
        revoked = dict(conn.execute(text("SELECT name, revoked_at FROM api_keys")).all())
        revokers = dict(conn.execute(text("SELECT name, revoked_by_entity_id FROM api_keys")).all())
        comments = dict(conn.execute(text("""
            SELECT attname, col_description('api_keys'::regclass, attnum)
            FROM pg_attribute
            WHERE attrelid = 'api_keys'::regclass AND attname IN ('revoked_at', 'revoked_by_entity_id')
        """)).all())
        fk = conn.execute(text("""
            SELECT con.confdeltype FROM pg_constraint con
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
            WHERE con.contype = 'f' AND con.conrelid = 'api_keys'::regclass
              AND att.attname = 'revoked_by_entity_id'
        """)).scalar()
    assert revoked == {"live": None, "used_then_revoked": _USED, "never_used_revoked": _CREATED}
    # The revoker of a key revoked before this revision is unknown.
    assert revokers == {"live": None, "used_then_revoked": None, "never_used_revoked": None}
    assert comments == {"revoked_at": _REVOKED_AT_COMMENT, "revoked_by_entity_id": _REVOKED_BY_COMMENT}
    assert fk == "n", f"revoked_by_entity_id FK uses ON DELETE type {fk!r}, expected 'n' (SET NULL)"
    engine.dispose()

    flask_db(url, "downgrade", _BEFORE)
    assert "revoked_at" not in _columns(engine)
    assert "revoked_by_entity_id" not in _columns(engine)
    with engine.connect() as conn:
        active = dict(conn.execute(text("SELECT name, active FROM api_keys")).all())
        nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'api_keys' AND column_name = 'active'"
        )).scalar()
    assert active == {"live": True, "used_then_revoked": False, "never_used_revoked": False}
    assert nullable == "NO"
