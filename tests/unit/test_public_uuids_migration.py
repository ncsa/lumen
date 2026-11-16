"""Round-trip the revision that adds the opaque public uuid columns, on SQLite."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "7456ebd042ef_add_public_uuids.py"
)
TABLES = ("entities", "groups", "api_keys", "conversations", "auth_requests")


def _revision_module():
    spec = importlib.util.spec_from_file_location("public_uuids_revision", _REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            with Operations.context(ctx):
                fn()
        conn.commit()


@pytest.fixture
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'public_uuids_rev.db'}")
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, name VARCHAR(32))"))
            conn.execute(sa.text(f"INSERT INTO {table} (id, name) VALUES (1, 'a'), (2, 'b'), (7, 'c')"))
    yield engine
    engine.dispose()


def test_upgrade_backfills_unique_uuids_and_downgrade_drops_them(engine, monkeypatch):
    module = _revision_module()
    # A batch smaller than the row count exercises more than one round trip.
    monkeypatch.setattr(module, "_BATCH", 2)
    _run(engine, module.upgrade)

    inspector = sa.inspect(engine)
    seen = []
    for table in TABLES:
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        assert columns["uuid"]["nullable"] is False
        assert [(i["name"], i["column_names"], bool(i["unique"])) for i in inspector.get_indexes(table)] == [
            (f"ix_{table}_uuid", ["uuid"], True)
        ]
        with engine.connect() as conn:
            rows = conn.execute(sa.text(f"SELECT id, name, uuid FROM {table} ORDER BY id")).all()
        assert [(r.id, r.name) for r in rows] == [(1, "a"), (2, "b"), (7, "c")]
        values = [r.uuid for r in rows]
        assert all(values)
        seen.extend(values)
    # Every row of every table has its own uuid.
    assert len(set(seen)) == len(seen) == 3 * len(TABLES)

    # The unique index refuses a duplicate.
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE groups SET uuid = (SELECT uuid FROM groups WHERE id = 1) WHERE id = 2"))

    _run(engine, module.downgrade)
    inspector = sa.inspect(engine)
    for table in TABLES:
        assert "uuid" not in {c["name"] for c in inspector.get_columns(table)}
        assert inspector.get_indexes(table) == []
        with engine.connect() as conn:
            assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() == 3
