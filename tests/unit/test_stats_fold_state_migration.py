"""Round-trip the stats_fold_state revision on SQLite.

SQLite is a single process, so the revision cuts it over at migration time:
one row with ``cutover_at = folded_until`` set to now. The PostgreSQL branch,
which leaves both columns NULL until an operator cuts over, is covered in
``tests/integration/test_migrations_postgres.py``.
"""
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lumen.timeutils import utcnow

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "e4f5a6b7c8d9_stats_fold_state.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location("stats_fold_state_revision", _REVISION_PATH)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stats_fold_state_rev.db'}")
    yield engine
    engine.dispose()


def test_upgrade_cuts_sqlite_over_at_migration_time(engine):
    before = utcnow()
    _run(engine, _revision_module().upgrade)
    after = utcnow()

    table = sa.Table("stats_fold_state", sa.MetaData(),
                     sa.Column("id", sa.Integer), sa.Column("cutover_at", sa.DateTime),
                     sa.Column("folded_until", sa.DateTime))
    with engine.connect() as conn:
        rows = conn.execute(sa.select(table)).all()
    assert len(rows) == 1
    row_id, cutover_at, folded_until = rows[0]
    assert row_id == 1
    assert cutover_at == folded_until
    assert before - timedelta(seconds=1) <= cutover_at <= after + timedelta(seconds=1)


def test_downgrade_drops_the_table(engine):
    module = _revision_module()
    _run(engine, module.upgrade)
    _run(engine, module.downgrade)

    assert "stats_fold_state" not in sa.inspect(engine).get_table_names()
