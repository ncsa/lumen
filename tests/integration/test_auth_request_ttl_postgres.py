"""``auth_requests.ttl`` as the migration chain builds it.

The SQLite suite builds the schema with ``create_all`` and never runs Alembic,
so leaving existing requests with a NULL ttl and the column comment are only
provable here, on a database the real chain migrated over existing rows.
"""
from datetime import datetime

import pytest
from sqlalchemy import text

from .conftest import flask_db

pytestmark = pytest.mark.postgres

_BEFORE = "b1c2d3e4f5a6"
_REVISION = "c2d3e4f5a6b7"
_CREATED = datetime(2026, 1, 1, 12, 0, 0)
_TTL_COMMENT = (
    "Key TTL in seconds (0 = never): as requested (null if none was), "
    "replaced by the approver's choice at approval"
)


def _seed(engine):
    with engine.begin() as conn:
        for code, status in [("AAAA-AAAA", "pending"), ("BBBB-BBBB", "approved")]:
            conn.execute(text(
                "INSERT INTO auth_requests (flow, client_id, requested_name, status, created_at,"
                " expires_at, user_code, overwrite)"
                " VALUES ('device', 'lumen-cli', 'opencode', :status, :t, :t, :code, false)"
            ), {"status": status, "t": _CREATED, "code": code})


def _ttl_comment(conn):
    return conn.execute(text("""
        SELECT col_description('auth_requests'::regclass, attnum)
        FROM pg_attribute WHERE attrelid = 'auth_requests'::regclass AND attname = 'ttl'
    """)).scalar_one_or_none()


def test_upgrade_leaves_existing_requests_null_and_downgrade_drops_column(pg_blank):
    url, engine = pg_blank
    flask_db(url, "upgrade", _BEFORE)
    _seed(engine)
    engine.dispose()

    flask_db(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        ttls = dict(conn.execute(text("SELECT user_code, ttl FROM auth_requests")).all())
        assert _ttl_comment(conn) == _TTL_COMMENT
    assert ttls == {"AAAA-AAAA": None, "BBBB-BBBB": None}
    engine.dispose()

    flask_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        columns = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'auth_requests'"
        )).scalars())
        assert conn.execute(text("SELECT COUNT(*) FROM auth_requests")).scalar() == 2
    assert "ttl" not in columns
