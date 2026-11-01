"""Round-trip the stats_deferred_marker revision on SQLite.

SQLite was cut over by ``e4f5a6b7c8d9``, so here the revision only adds the
column and must leave that cutover alone. The PostgreSQL side (the automatic
cutover, an existing cutover kept, a compressed chunk) is covered in
``tests/integration/test_migrations_postgres.py`` and
``tests/integration/test_stats_deferred_migration_postgres.py``.
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
    / "migrations" / "versions" / "f5b6c7d8e9f0_stats_deferred_marker.py"
)

CUTOVER = datetime(2026, 10, 1, 12, 0, 0)
FOLDED = datetime(2026, 10, 1, 13, 0, 0)


def _revision_module():
    spec = importlib.util.spec_from_file_location("stats_deferred_marker_revision", _REVISION_PATH)
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


_metadata = sa.MetaData()
_request_logs = sa.Table(
    "request_logs", _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("time", sa.DateTime(timezone=True)),
    sa.Column("source", sa.String(8)),
)
_fold_state = sa.Table(
    "stats_fold_state", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("cutover_at", sa.DateTime),
    sa.Column("folded_until", sa.DateTime),
)


@pytest.fixture
def engine(tmp_path):
    """The two tables as the previous revision leaves them on SQLite: cut over, one row logged."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stats_deferred_marker_rev.db'}")
    _metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(_fold_state.insert().values(id=1, cutover_at=CUTOVER, folded_until=FOLDED))
        conn.execute(_request_logs.insert().values(id=1, time=CUTOVER, source="api"))
    yield engine
    engine.dispose()


def _columns(engine):
    return {c["name"]: c for c in sa.inspect(engine).get_columns("request_logs")}


def _fold_state_rows(engine):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.select(_fold_state))]


def test_upgrade_adds_a_nullable_column_and_keeps_the_sqlite_cutover(engine):
    _run(engine, _revision_module().upgrade)

    column = _columns(engine)["stats_deferred"]
    assert column["nullable"] is True
    assert column["default"] is None
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT stats_deferred FROM request_logs")).scalar_one() is None
    assert _fold_state_rows(engine) == [(1, CUTOVER, FOLDED)]


def test_downgrade_drops_the_column(engine):
    module = _revision_module()
    _run(engine, module.upgrade)
    _run(engine, module.downgrade)

    assert "stats_deferred" not in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM request_logs")).scalar_one() == 1
    assert _fold_state_rows(engine) == [(1, CUTOVER, FOLDED)]
