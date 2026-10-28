"""The counter-table fillfactor revision is a no-op on SQLite.

``fillfactor`` is a PostgreSQL storage parameter; SQLite has no equivalent and
rejects ``ALTER TABLE ... SET``. The revision must therefore run nothing on
SQLite in either direction. The PostgreSQL side is covered in
``tests/integration/test_counter_tables_fillfactor_postgres.py``.
"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "d3e4f5a6b7c8_counter_tables_fillfactor.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location("counter_tables_fillfactor_revision", _REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(engine, fn):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with ctx.begin_transaction():
                with Operations.context(ctx):
                    fn()
            conn.commit()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    return statements


def test_upgrade_and_downgrade_run_nothing_on_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fillfactor_rev.db'}")
    module = _revision_module()
    try:
        assert _statements(engine, module.upgrade) == []
        assert _statements(engine, module.downgrade) == []
    finally:
        engine.dispose()
