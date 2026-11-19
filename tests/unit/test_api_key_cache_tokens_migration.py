"""Round-trip the revision that adds per-key cache token totals, on SQLite."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "f4a5b6c7d8e9_add_api_key_cache_tokens.py"
)
_TOTALS = ("cached_input_tokens", "cache_write_tokens")


def _revision_module():
    spec = importlib.util.spec_from_file_location("api_key_cache_tokens_revision", _REVISION_PATH)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'api_key_cache_tokens_rev.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, input_tokens BIGINT NOT NULL DEFAULT 0)"
        ))
        conn.execute(sa.text(
            "CREATE TABLE request_logs (id INTEGER PRIMARY KEY, api_key_id INTEGER, "
            "cached_input_tokens INTEGER, cache_write_tokens INTEGER)"
        ))
        conn.execute(sa.text("INSERT INTO api_keys VALUES (1, 500), (2, 300), (3, 0)"))
        conn.execute(sa.text(
            "INSERT INTO request_logs (api_key_id, cached_input_tokens, cache_write_tokens) VALUES "
            # Non-NULL counts the upgrade must not sum into the new totals.
            "(1, 40, 10), (1, NULL, 5), (2, NULL, NULL), (NULL, 1000, 1000)"
        ))
    yield engine
    engine.dispose()


def _columns(engine, table):
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_starts_totals_at_zero_and_downgrade_drops_them(engine):
    module = _revision_module()
    _run(engine, module.upgrade)

    columns = _columns(engine, "api_keys")
    for column in _TOTALS:
        assert column in columns
        assert columns[column]["nullable"] is False
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, input_tokens, cached_input_tokens, cache_write_tokens FROM api_keys ORDER BY id"
        )).all()
    # Every existing key starts at 0, even with counts in request_logs: the
    # upgrade does not read the request history.
    assert [tuple(r) for r in rows] == [(1, 500, 0, 0), (2, 300, 0, 0), (3, 0, 0, 0)]

    # A key created after the upgrade starts at 0 from the server default.
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO api_keys (id) VALUES (4)"))
        assert conn.execute(sa.text(
            "SELECT cached_input_tokens, cache_write_tokens FROM api_keys WHERE id = 4"
        )).one() == (0, 0)

    _run(engine, module.downgrade)
    assert set(_columns(engine, "api_keys")) & set(_TOTALS) == set()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM api_keys")).scalar() == 4
