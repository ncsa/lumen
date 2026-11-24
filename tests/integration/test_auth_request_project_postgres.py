"""``auth_requests.requested_project``, ``project_entity_id`` and ``denial_reason``
as the migration chain builds them.

The SQLite suite builds the schema with ``create_all`` and never runs Alembic,
so the column types and comments, the project FK's ``ON DELETE CASCADE``, and
existing rows staying personal requests with no reason are only provable here.
"""
from datetime import datetime

import pytest
from sqlalchemy import text

from lumen.models.auth_request import AuthRequest

from .conftest import migrate_db

pytestmark = pytest.mark.postgres

_BEFORE = "b7c9d1e3f5a7"
_REVISION = "e9a1c3d5f7b2"
_CREATED = datetime(2026, 11, 1, 12, 0, 0)
_COLUMNS = {
    "requested_project": "character varying(256)",
    "project_entity_id": "integer",
    "denial_reason": "character varying(512)",
}


def _columns(conn):
    return {
        name: (sql_type, comment)
        for name, sql_type, comment in conn.execute(text("""
            SELECT attname, format_type(atttypid, atttypmod), col_description('auth_requests'::regclass, attnum)
            FROM pg_attribute
            WHERE attrelid = 'auth_requests'::regclass AND attnum > 0 AND NOT attisdropped
        """)).all()
    }


def test_upgrade_adds_nullable_project_columns_and_downgrade_drops_them(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    migrate_db(url, "downgrade", _BEFORE)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO auth_requests (flow, client_id, requested_name, status, created_at,"
            " expires_at, user_code, overwrite)"
            " VALUES ('device', 'lumen-cli', 'opencode', 'pending', :t, :t, 'AAAA-AAAA', false)"
        ), {"t": _CREATED})
    engine.dispose()

    migrate_db(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        columns = _columns(conn)
        for name, sql_type in _COLUMNS.items():
            assert columns[name] == (sql_type, AuthRequest.__table__.columns[name].comment)
        assert conn.execute(text(
            "SELECT requested_project, project_entity_id, denial_reason FROM auth_requests"
        )).one() == (None, None, None)
        fk = conn.execute(text("""
            SELECT con.confdeltype, con.confrelid::regclass::text FROM pg_constraint con
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
            WHERE con.contype = 'f' AND con.conrelid = 'auth_requests'::regclass
              AND att.attname = 'project_entity_id'
        """)).one()
        assert tuple(fk) == ("c", "entities")
    engine.dispose()

    migrate_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        columns = _columns(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM auth_requests")).scalar() == 1
    assert not set(_COLUMNS) & set(columns)
    engine.dispose()

    migrate_db(url, "upgrade", "head")

