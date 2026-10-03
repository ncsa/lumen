"""Round-trip the created_by_entity_id revision on SQLite.

The full chain is PostgreSQL-only (migrations/env.py blocks SQLite upgrades
outright), but each revision must stay dialect-portable so the chain remains
repaired-able. This executes the new revision's upgrade()/downgrade() directly
against a SQLite database shaped like it stood at the previous head.
"""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "d6e7f8a9b0c1_add_api_key_created_by.py"
)

# api_keys exactly as the previous head (q1w2e3r4t5y6) left it, constraints
# named as the migrated chain names them (batch recreate needs names to copy).
_OLD_API_KEYS = """
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY,
        entity_id INTEGER NOT NULL,
        name VARCHAR(128) NOT NULL,
        key_hash VARCHAR(64) NOT NULL,
        key_hint VARCHAR(32),
        active BOOLEAN NOT NULL,
        requests INTEGER NOT NULL,
        input_tokens BIGINT NOT NULL,
        output_tokens BIGINT NOT NULL,
        audio_seconds BIGINT NOT NULL,
        cost NUMERIC(12, 6) NOT NULL,
        last_used_at DATETIME,
        created_at DATETIME,
        client_id VARCHAR(128),
        requested_by VARCHAR(128),
        CONSTRAINT api_keys_entity_id_fkey FOREIGN KEY (entity_id)
            REFERENCES entities (id) ON DELETE CASCADE,
        CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash)
    )
"""


def _revision_module():
    spec = importlib.util.spec_from_file_location("created_by_revision", _REVISION_PATH)
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
        conn.exec_driver_sql(
            "INSERT INTO api_keys (entity_id, name, key_hash, active, requests,"
            " input_tokens, output_tokens, audio_seconds, cost)"
            " VALUES (1, 'legacy', 'aa', 1, 0, 0, 0, 0, 0)"
        )
    yield engine
    engine.dispose()


def _run(engine, fn):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            with Operations.context(ctx):
                fn()
        conn.commit()


def _columns(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("api_keys")}


def test_upgrade_adds_nullable_column_with_set_null_fk(pre_migration_sqlite):
    module = _revision_module()
    _run(pre_migration_sqlite, module.upgrade)

    assert "created_by_entity_id" in _columns(pre_migration_sqlite)
    col = next(c for c in sa.inspect(pre_migration_sqlite).get_columns("api_keys")
               if c["name"] == "created_by_entity_id")
    assert col["nullable"] is True

    fks = [fk for fk in sa.inspect(pre_migration_sqlite).get_foreign_keys("api_keys")
           if fk["referred_table"] == "entities" and fk["constrained_columns"] == ["created_by_entity_id"]]
    assert fks, "no FK from created_by_entity_id to entities.id"
    assert fks[0]["options"].get("ondelete") == "SET NULL"

    # The pre-existing key is untouched and, like every legacy key, unattributed.
    with pre_migration_sqlite.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT name, created_by_entity_id FROM api_keys WHERE name = 'legacy'"
        )).one()
    assert row.created_by_entity_id is None


def test_downgrade_drops_column_and_fk(pre_migration_sqlite):
    module = _revision_module()
    _run(pre_migration_sqlite, module.upgrade)
    _run(pre_migration_sqlite, module.downgrade)

    assert "created_by_entity_id" not in _columns(pre_migration_sqlite)
    assert not [fk for fk in sa.inspect(pre_migration_sqlite).get_foreign_keys("api_keys")
                if fk["constrained_columns"] == ["created_by_entity_id"]]
    with pre_migration_sqlite.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM api_keys")).scalar() == 1
