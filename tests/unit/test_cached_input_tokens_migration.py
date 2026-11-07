"""Round-trip the revision that adds cached_input_tokens, on SQLite."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "d2e3f4a5b6c7_cached_input_tokens.py"
)
_TOTALS = {"cached_input_tokens", "folded_cached_input_tokens"}


def _revision_module():
    spec = importlib.util.spec_from_file_location("cached_input_tokens_revision", _REVISION_PATH)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'cached_input_tokens_rev.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE request_logs (id INTEGER PRIMARY KEY, input_tokens INTEGER NOT NULL DEFAULT 0)"
        ))
        conn.execute(sa.text("CREATE TABLE model_stats (id INTEGER PRIMARY KEY, requests INTEGER)"))
        conn.execute(sa.text("CREATE TABLE entity_stats (entity_id INTEGER PRIMARY KEY, requests INTEGER)"))
        conn.execute(sa.text("INSERT INTO request_logs VALUES (1, 100)"))
        conn.execute(sa.text("INSERT INTO model_stats VALUES (1, 7)"))
        conn.execute(sa.text("INSERT INTO entity_stats VALUES (1, 7)"))
    yield engine
    engine.dispose()


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _nullable(engine, table, column):
    return {c["name"]: c["nullable"] for c in sa.inspect(engine).get_columns(table)}[column]


def test_upgrade_adds_the_split_and_downgrade_drops_it(engine):
    module = _revision_module()
    _run(engine, module.upgrade)

    # The per-request count is nullable: an existing row has nothing to record,
    # and NULL is what says so rather than a fabricated zero.
    assert "cached_input_tokens" in _columns(engine, "request_logs")
    assert _nullable(engine, "request_logs", "cached_input_tokens") is True
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT cached_input_tokens FROM request_logs")).scalar() is None

    # The totals are counters, so existing rows start at zero and are not NULL.
    for table in ("model_stats", "entity_stats"):
        assert _TOTALS <= _columns(engine, table)
        for column in _TOTALS:
            assert _nullable(engine, table, column) is False
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                f"SELECT requests, {', '.join(sorted(_TOTALS))} FROM {table}"
            )).one()
        assert row == (7, 0, 0)

    _run(engine, module.downgrade)
    assert _columns(engine, "request_logs") & {"cached_input_tokens"} == set()
    for table in ("model_stats", "entity_stats"):
        assert _columns(engine, table) & _TOTALS == set()
