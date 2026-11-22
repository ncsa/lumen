"""Round-trip the api_keys.key_type revision on SQLite.

The full chain is PostgreSQL-only (see
``tests/integration/test_api_key_type_migration_postgres.py``), but each
revision must stay dialect-portable. This executes the revision's
upgrade()/downgrade() directly against a SQLite database whose api_keys table
is the current model minus ``key_type``, as the previous head left it.
"""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from lumen.models.api_key import APIKey

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "b7c9d1e3f5a7_api_key_type.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location("api_key_type_revision", _REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_api_keys(metadata):
    """api_keys as the model defines it, without key_type and its CHECK."""
    current = APIKey.__table__
    return sa.Table(
        "api_keys", metadata,
        *[sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
          for c in current.columns if c.name != "key_type"],
    )


@pytest.fixture
def pre_migration_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'api_key_type_rev.db'}")
    metadata = sa.MetaData()
    sa.Table("entities", metadata, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("entity_type", sa.String(16)))
    _old_api_keys(metadata)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO entities (id, entity_type) VALUES (1, 'user'), (2, 'project')")
        for name, entity_id, client_id in [("user", 1, None), ("project", 2, None), ("oauth", 1, "cli")]:
            conn.execute(sa.text(
                "INSERT INTO api_keys (uuid, entity_id, name, key_hash, client_id, requests, input_tokens,"
                " output_tokens, cached_input_tokens, cache_write_tokens, audio_seconds, cost)"
                " VALUES (:name, :e, :name, :name, :c, 0, 0, 0, 0, 0, 0, 0)"
            ), {"name": name, "e": entity_id, "c": client_id})
    yield engine
    engine.dispose()


def _run(engine, fn):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            with Operations.context(ctx):
                fn()
        conn.commit()


def test_upgrade_makes_every_existing_key_normal(pre_migration_sqlite):
    _run(pre_migration_sqlite, _revision_module().upgrade)

    with pre_migration_sqlite.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT name, key_type FROM api_keys")).all())
    assert rows == {"user": "normal", "project": "normal", "oauth": "normal"}
    col = next(c for c in sa.inspect(pre_migration_sqlite).get_columns("api_keys") if c["name"] == "key_type")
    assert col["nullable"] is False
    checks = sa.inspect(pre_migration_sqlite).get_check_constraints("api_keys")
    assert [c["name"] for c in checks] == ["ck_api_keys_key_type"]


def test_upgrade_check_refuses_unknown_types_and_defaults_to_normal(pre_migration_sqlite):
    _run(pre_migration_sqlite, _revision_module().upgrade)

    insert = ("INSERT INTO api_keys (uuid, entity_id, name, key_hash, requests, input_tokens, output_tokens,"
              " cached_input_tokens, cache_write_tokens, audio_seconds, cost{col})"
              " VALUES (:n, 1, :n, :n, 0, 0, 0, 0, 0, 0, 0{val})")
    with pre_migration_sqlite.begin() as conn:
        conn.execute(sa.text(insert.format(col="", val="")), {"n": "new"})
        conn.execute(sa.text(insert.format(col=", key_type", val=", 'monitoring'")), {"n": "mon"})
        rows = dict(conn.execute(sa.text("SELECT name, key_type FROM api_keys WHERE name IN ('new', 'mon')")).all())
    assert rows == {"new": "normal", "mon": "monitoring"}
    with pytest.raises(IntegrityError, match="ck_api_keys_key_type"):
        with pre_migration_sqlite.begin() as conn:
            conn.execute(sa.text(insert.format(col=", key_type", val=", 'root'")), {"n": "bad"})


def test_downgrade_drops_the_column(pre_migration_sqlite):
    module = _revision_module()
    _run(pre_migration_sqlite, module.upgrade)
    _run(pre_migration_sqlite, module.downgrade)

    assert "key_type" not in {c["name"] for c in sa.inspect(pre_migration_sqlite).get_columns("api_keys")}
    assert sa.inspect(pre_migration_sqlite).get_check_constraints("api_keys") == []
    with pre_migration_sqlite.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM api_keys")).scalar() == 3
