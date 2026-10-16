"""Round-trip the api_keys.expires_at revision on SQLite.

The full chain is PostgreSQL-only, but each revision must stay dialect-portable.
This executes the revision's upgrade()/downgrade() directly against a SQLite
database shaped like api_keys stood at the previous head.
"""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "b1c2d3e4f5a6_api_key_expires_at.py"
)

# api_keys exactly as the previous head (a827b0223638) left it.
_OLD_API_KEYS = """
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY,
        entity_id INTEGER NOT NULL,
        name VARCHAR(128) NOT NULL,
        key_hash VARCHAR(64) NOT NULL,
        key_hint VARCHAR(32),
        requests INTEGER NOT NULL,
        input_tokens BIGINT NOT NULL,
        output_tokens BIGINT NOT NULL,
        audio_seconds BIGINT NOT NULL,
        cost NUMERIC(12, 6) NOT NULL,
        last_used_at DATETIME,
        created_at DATETIME,
        client_id VARCHAR(128),
        requested_by VARCHAR(128),
        created_by_entity_id INTEGER,
        revoked_at DATETIME,
        revoked_by_entity_id INTEGER,
        CONSTRAINT api_keys_entity_id_fkey FOREIGN KEY (entity_id)
            REFERENCES entities (id) ON DELETE CASCADE,
        CONSTRAINT api_keys_created_by_entity_id_fkey FOREIGN KEY (created_by_entity_id)
            REFERENCES entities (id) ON DELETE SET NULL,
        CONSTRAINT api_keys_revoked_by_entity_id_fkey FOREIGN KEY (revoked_by_entity_id)
            REFERENCES entities (id) ON DELETE SET NULL,
        CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash)
    )
"""

_CREATED = datetime(2026, 1, 1, 12, 0, 0)


def _revision_module():
    spec = importlib.util.spec_from_file_location("expires_at_revision", _REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pre_migration_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'api_keys_rev.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE entities (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(_OLD_API_KEYS)
        conn.exec_driver_sql("INSERT INTO entities (id) VALUES (1)")
        for name, revoked_at in [("live", None), ("revoked", _CREATED)]:
            conn.execute(sa.text(
                "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
                " output_tokens, audio_seconds, cost, created_at, revoked_at)"
                " VALUES (1, :name, :name, 0, 0, 0, 0, 0, :created, :revoked_at)"
            ), {"name": name, "created": _CREATED, "revoked_at": revoked_at})
    yield engine
    engine.dispose()


def _run(engine, fn):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            with Operations.context(ctx):
                fn()
        conn.commit()


def test_upgrade_adds_nullable_indexed_expires_at_and_keeps_keys_unexpiring(pre_migration_sqlite):
    _run(pre_migration_sqlite, _revision_module().upgrade)

    inspector = sa.inspect(pre_migration_sqlite)
    col = next(c for c in inspector.get_columns("api_keys") if c["name"] == "expires_at")
    assert col["nullable"] is True
    assert any(ix["name"] == "ix_api_keys_expires_at" and ix["column_names"] == ["expires_at"]
               for ix in inspector.get_indexes("api_keys"))
    with pre_migration_sqlite.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT name, expires_at FROM api_keys")).all())
    assert rows == {"live": None, "revoked": None}


def test_downgrade_drops_expires_at(pre_migration_sqlite):
    module = _revision_module()
    _run(pre_migration_sqlite, module.upgrade)
    _run(pre_migration_sqlite, module.downgrade)

    inspector = sa.inspect(pre_migration_sqlite)
    assert "expires_at" not in {c["name"] for c in inspector.get_columns("api_keys")}
    assert "ix_api_keys_expires_at" not in {ix["name"] for ix in inspector.get_indexes("api_keys")}
    with pre_migration_sqlite.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM api_keys")).scalar() == 2
