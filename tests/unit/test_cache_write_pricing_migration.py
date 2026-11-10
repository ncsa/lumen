"""Round-trip the revision that adds cache-write pricing and tokens, on SQLite."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "e3f4a5b6c7d8_cache_write_pricing.py"
)
_TOTALS = {"cache_write_tokens", "folded_cache_write_tokens"}


def _revision_module():
    spec = importlib.util.spec_from_file_location("cache_write_pricing_revision", _REVISION_PATH)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'cache_write_rev.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE model_configs ("
            "id INTEGER PRIMARY KEY, input_cost_per_million NUMERIC(12, 6))"
        ))
        conn.execute(sa.text(
            "CREATE TABLE request_logs (id INTEGER PRIMARY KEY, input_tokens INTEGER NOT NULL DEFAULT 0)"
        ))
        conn.execute(sa.text("CREATE TABLE model_stats (id INTEGER PRIMARY KEY, requests INTEGER)"))
        conn.execute(sa.text("CREATE TABLE entity_stats (entity_id INTEGER PRIMARY KEY, requests INTEGER)"))
        conn.execute(sa.text("INSERT INTO model_configs VALUES (1, 2.0)"))
        conn.execute(sa.text("INSERT INTO request_logs VALUES (1, 100)"))
        conn.execute(sa.text("INSERT INTO model_stats VALUES (1, 7)"))
        conn.execute(sa.text("INSERT INTO entity_stats VALUES (1, 7)"))
    yield engine
    engine.dispose()


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _nullable(engine, table, column):
    return {c["name"]: c["nullable"] for c in sa.inspect(engine).get_columns(table)}[column]


def test_upgrade_adds_the_write_side_and_downgrade_drops_it(engine):
    module = _revision_module()
    _run(engine, module.upgrade)

    # The price is nullable: an existing model keeps charging the input rate
    # until an admin opts in, which is what NULL says.
    assert "cache_write_cost_per_million" in _columns(engine, "model_configs")
    assert _nullable(engine, "model_configs", "cache_write_cost_per_million") is True
    with engine.connect() as conn:
        assert conn.execute(
            sa.text("SELECT cache_write_cost_per_million FROM model_configs")
        ).scalar() is None

    # The per-request count is nullable too: a pre-existing row has nothing to
    # record, and NULL is what says so rather than a fabricated zero.
    assert "cache_write_tokens" in _columns(engine, "request_logs")
    assert _nullable(engine, "request_logs", "cache_write_tokens") is True
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT cache_write_tokens FROM request_logs")).scalar() is None

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
    assert _columns(engine, "model_configs") & {"cache_write_cost_per_million"} == set()
    assert _columns(engine, "request_logs") & {"cache_write_tokens"} == set()
    for table in ("model_stats", "entity_stats"):
        assert _columns(engine, table) & _TOTALS == set()
