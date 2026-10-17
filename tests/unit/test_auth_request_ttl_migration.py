"""Round-trip the auth_requests.ttl revision on SQLite.

The full chain is PostgreSQL-only, but each revision must stay dialect-portable.
This executes the revision's upgrade()/downgrade() directly against a SQLite
database shaped like auth_requests stood at the previous head.
"""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lumen.models.auth_request import AuthRequest

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "c2d3e4f5a6b7_auth_request_ttl.py"
)

# auth_requests exactly as the previous head (b1c2d3e4f5a6) left it.
_OLD_AUTH_REQUESTS = [
    """
    CREATE TABLE auth_requests (
        id INTEGER PRIMARY KEY,
        flow VARCHAR(16) NOT NULL,
        client_id VARCHAR(128) NOT NULL,
        requested_name VARCHAR(128) NOT NULL,
        author VARCHAR(128),
        status VARCHAR(16) DEFAULT 'pending' NOT NULL,
        created_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        device_code_hash VARCHAR(64),
        user_code VARCHAR(9),
        last_polled_at DATETIME,
        redirect_uri TEXT,
        code_challenge VARCHAR(128),
        auth_code_hash VARCHAR(64),
        auth_code_expires_at DATETIME,
        entity_id INTEGER,
        overwrite BOOLEAN DEFAULT 0 NOT NULL,
        approved_at DATETIME,
        api_key_id INTEGER,
        CONSTRAINT ck_auth_requests_flow CHECK (flow IN ('device', 'code')),
        CONSTRAINT ck_auth_requests_status CHECK (status IN ('pending', 'approved', 'denied', 'claimed')),
        FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_auth_requests_expires_at ON auth_requests (expires_at)",
    "CREATE UNIQUE INDEX uq_auth_requests_device_code_hash ON auth_requests (device_code_hash)",
    "CREATE UNIQUE INDEX uq_auth_requests_auth_code_hash ON auth_requests (auth_code_hash)",
    "CREATE UNIQUE INDEX uq_auth_requests_pending_user_code ON auth_requests (user_code) WHERE status = 'pending'",
]

_CREATED = datetime(2026, 1, 1, 12, 0, 0)
_INDEXES = {
    "ix_auth_requests_expires_at", "uq_auth_requests_device_code_hash",
    "uq_auth_requests_auth_code_hash", "uq_auth_requests_pending_user_code",
}


def _revision_module():
    spec = importlib.util.spec_from_file_location("auth_request_ttl_revision", _REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pre_migration_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'auth_requests_rev.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE entities (id INTEGER PRIMARY KEY)")
        for ddl in _OLD_AUTH_REQUESTS:
            conn.exec_driver_sql(ddl)
        for code, status in [("AAAA-AAAA", "pending"), ("BBBB-BBBB", "approved")]:
            conn.execute(sa.text(
                "INSERT INTO auth_requests (flow, client_id, requested_name, status, created_at,"
                " expires_at, user_code) VALUES ('device', 'lumen-cli', 'opencode', :status, :t, :t, :code)"
            ), {"status": status, "t": _CREATED, "code": code})
    yield engine
    engine.dispose()


def _run(engine, fn):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            with Operations.context(ctx):
                fn()
        conn.commit()


def test_upgrade_adds_nullable_ttl_and_leaves_existing_requests_null(pre_migration_sqlite):
    _run(pre_migration_sqlite, _revision_module().upgrade)

    inspector = sa.inspect(pre_migration_sqlite)
    col = next(c for c in inspector.get_columns("auth_requests") if c["name"] == "ttl")
    assert col["nullable"] is True
    # BIGINT, like the model: allowed TTLs (up to 100 years) overflow a 32-bit integer.
    assert isinstance(col["type"], sa.BigInteger)
    assert isinstance(AuthRequest.__table__.c.ttl.type, sa.BigInteger)
    assert _INDEXES <= {ix["name"] for ix in inspector.get_indexes("auth_requests")}
    with pre_migration_sqlite.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT user_code, ttl FROM auth_requests")).all())
    assert rows == {"AAAA-AAAA": None, "BBBB-BBBB": None}


def test_downgrade_drops_ttl_and_keeps_rows_and_indexes(pre_migration_sqlite):
    module = _revision_module()
    _run(pre_migration_sqlite, module.upgrade)
    _run(pre_migration_sqlite, module.downgrade)

    inspector = sa.inspect(pre_migration_sqlite)
    assert "ttl" not in {c["name"] for c in inspector.get_columns("auth_requests")}
    assert _INDEXES <= {ix["name"] for ix in inspector.get_indexes("auth_requests")}
    with pre_migration_sqlite.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM auth_requests")).scalar() == 2
