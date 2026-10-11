"""Round-trip the api_keys.revoked_at / revoked_by_entity_id revision on SQLite.

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
    / "migrations" / "versions" / "a9b0c1d2e3f4_api_key_revoked_at.py"
)

# api_keys exactly as the previous head (f8a9b0c1d2e3) left it.
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
        created_by_entity_id INTEGER,
        CONSTRAINT api_keys_entity_id_fkey FOREIGN KEY (entity_id)
            REFERENCES entities (id) ON DELETE CASCADE,
        CONSTRAINT api_keys_created_by_entity_id_fkey FOREIGN KEY (created_by_entity_id)
            REFERENCES entities (id) ON DELETE SET NULL,
        CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash)
    )
"""

_CREATED = datetime(2026, 1, 1, 12, 0, 0)
_USED = datetime(2026, 2, 1, 12, 0, 0)


def _revision_module():
    spec = importlib.util.spec_from_file_location("revoked_at_revision", _REVISION_PATH)
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
        for name, active, last_used in [
            ("live", True, _USED),
            ("used_then_revoked", False, _USED),
            ("never_used_revoked", False, None),
        ]:
            conn.execute(sa.text(
                "INSERT INTO api_keys (entity_id, name, key_hash, active, requests,"
                " input_tokens, output_tokens, audio_seconds, cost, last_used_at, created_at)"
                " VALUES (1, :name, :name, :active, 0, 0, 0, 0, 0, :last_used, :created)"
            ), {"name": name, "active": active, "last_used": last_used, "created": _CREATED})
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


def _revoked_at(engine):
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT name, revoked_at FROM api_keys")).all()
    return {name: (datetime.fromisoformat(v) if isinstance(v, str) else v) for name, v in rows}


def test_upgrade_backfills_revoked_at_and_drops_active(pre_migration_sqlite):
    _run(pre_migration_sqlite, _revision_module().upgrade)

    assert "active" not in _columns(pre_migration_sqlite)
    assert _revoked_at(pre_migration_sqlite) == {
        "live": None,
        "used_then_revoked": _USED,
        "never_used_revoked": _CREATED,
    }


def test_upgrade_adds_nullable_revoker_with_set_null_fk(pre_migration_sqlite):
    _run(pre_migration_sqlite, _revision_module().upgrade)

    col = next(c for c in sa.inspect(pre_migration_sqlite).get_columns("api_keys")
               if c["name"] == "revoked_by_entity_id")
    assert col["nullable"] is True
    fks = [fk for fk in sa.inspect(pre_migration_sqlite).get_foreign_keys("api_keys")
           if fk["constrained_columns"] == ["revoked_by_entity_id"]]
    assert fks and fks[0]["referred_table"] == "entities"
    assert fks[0]["options"].get("ondelete") == "SET NULL"
    # The revoker of a key revoked before this revision is unknown.
    with pre_migration_sqlite.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT COUNT(*) FROM api_keys WHERE revoked_by_entity_id IS NOT NULL"
        )).scalar() == 0


def test_upgrade_keeps_revoked_key_revoked_without_timestamps(pre_migration_sqlite):
    with pre_migration_sqlite.begin() as conn:
        conn.exec_driver_sql("UPDATE api_keys SET created_at = NULL WHERE name = 'never_used_revoked'")
    _run(pre_migration_sqlite, _revision_module().upgrade)

    assert _revoked_at(pre_migration_sqlite)["never_used_revoked"] is not None


def test_downgrade_restores_active(pre_migration_sqlite):
    module = _revision_module()
    _run(pre_migration_sqlite, module.upgrade)
    _run(pre_migration_sqlite, module.downgrade)

    assert "revoked_at" not in _columns(pre_migration_sqlite)
    assert "revoked_by_entity_id" not in _columns(pre_migration_sqlite)
    assert not [fk for fk in sa.inspect(pre_migration_sqlite).get_foreign_keys("api_keys")
                if fk["constrained_columns"] == ["revoked_by_entity_id"]]
    col = next(c for c in sa.inspect(pre_migration_sqlite).get_columns("api_keys") if c["name"] == "active")
    assert col["nullable"] is False
    with pre_migration_sqlite.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT name, active FROM api_keys")).all())
    assert rows == {"live": True, "used_then_revoked": False, "never_used_revoked": False}
