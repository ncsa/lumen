"""Round-trip the revision that adds the folded_* columns, on SQLite."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "a6b8c0d2e4f6_stats_folded_totals.py"
)
_FOLDED = {"folded_requests", "folded_input_tokens", "folded_output_tokens", "folded_audio_seconds", "folded_cost"}


def _revision_module():
    spec = importlib.util.spec_from_file_location("stats_folded_totals_revision", _REVISION_PATH)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stats_folded_totals_rev.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE model_stats (id INTEGER PRIMARY KEY, requests INTEGER)"))
        conn.execute(sa.text("CREATE TABLE entity_stats (entity_id INTEGER PRIMARY KEY, requests INTEGER)"))
        conn.execute(sa.text("INSERT INTO model_stats VALUES (1, 7)"))
        conn.execute(sa.text("INSERT INTO entity_stats VALUES (1, 7)"))
    yield engine
    engine.dispose()


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_adds_zeroed_columns_and_downgrade_drops_them(engine):
    module = _revision_module()
    _run(engine, module.upgrade)
    for table in ("model_stats", "entity_stats"):
        assert _FOLDED <= _columns(engine, table)
        with engine.connect() as conn:
            row = conn.execute(sa.text(f"SELECT requests, {', '.join(sorted(_FOLDED))} FROM {table}")).one()
        assert row == (7, 0, 0, 0, 0, 0), "existing rows start with no folded part"

    _run(engine, module.downgrade)
    for table in ("model_stats", "entity_stats"):
        assert _columns(engine, table) & _FOLDED == set()
