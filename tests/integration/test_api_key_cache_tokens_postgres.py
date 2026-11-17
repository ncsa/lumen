"""The api_key_cache_tokens revision on real PostgreSQL + TimescaleDB.

The test steps this module's database back to ``e3f4a5b6c7d8`` with the same
``flask db`` command the container runs, seeds request_logs rows with cache
counts for two keys and for no key, upgrades again, and checks that every key's
totals start at 0 (the upgrade does not read request_logs) and that both
columns carry their comment. It then downgrades and checks the columns are
gone.

A second test checks that the model and the migrated schema agree on the
columns, so ``db.create_all`` and an upgraded database build the same thing. It
runs first, on the untouched upgrade.
"""

import os
import subprocess

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from lumen.models.api_key import APIKey

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

PREVIOUS = "e3f4a5b6c7d8"
SOURCE = "ckmark"  # request_logs.source is VARCHAR(8)
COLUMNS = ("cached_input_tokens", "cache_write_tokens")


def _flask_db(url, *args):
    env = {**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"}
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"flask db {' '.join(args)} failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def _columns(conn):
    return {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'api_keys' AND column_name = ANY(:c)"
    ), {"c": list(COLUMNS)})}


def _key(conn, entity_id, name):
    return conn.execute(text(
        "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
        " output_tokens, audio_seconds, cost, created_at)"
        " VALUES (:e, :n, :n, 0, 0, 0, 0, 0, now()) RETURNING id"
    ), {"e": entity_id, "n": name}).scalar()


def _log(conn, api_key_id, cached, write):
    conn.execute(text(
        "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration,"
        " api_key_id, cached_input_tokens, cache_write_tokens)"
        " VALUES (now(), :s, 100, 1, 0.1, 0.1, :k, :c, :w)"
    ), {"s": SOURCE, "k": api_key_id, "c": cached, "w": write})


def test_autogenerate_proposes_no_api_key_cache_token_changes(pg_migrated_isolated):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), APIKey.metadata)
    # Diffs come as tuples, or lists of tuples for column modifications.
    flat = [d for diff in diffs for d in (diff if isinstance(diff, list) else [diff])]
    assert [d for d in flat if any(c in repr(d) for c in COLUMNS) and "api_keys" in repr(d)] == []


def test_upgrade_starts_totals_at_zero_and_downgrade_drops_them(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert _columns(conn) == set()

    with engine.begin() as conn:
        entity_id = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'cache-tokens', 'CT', true) RETURNING id"
        )).scalar()
        busy, quiet, unused = (_key(conn, entity_id, n) for n in ("ckbusy", "ckquiet", "ckunused"))
        # Cache counts the upgrade must not sum into the new totals.
        _log(conn, busy, 40, 10)
        _log(conn, busy, None, 5)
        _log(conn, quiet, None, None)
        _log(conn, None, 1000, 1000)

    _flask_db(url, "upgrade")

    with engine.connect() as conn:
        assert _columns(conn) == set(COLUMNS)
        rows = conn.execute(text(
            "SELECT id, cached_input_tokens, cache_write_tokens FROM api_keys "
            "WHERE id IN (:a, :b, :c) ORDER BY id"
        ), {"a": busy, "b": quiet, "c": unused}).all()
        assert [tuple(r) for r in rows] == [(busy, 0, 0), (quiet, 0, 0), (unused, 0, 0)]
        for column in COLUMNS:
            comment = conn.execute(text(
                "SELECT col_description('api_keys'::regclass, attnum) FROM pg_attribute "
                "WHERE attrelid = 'api_keys'::regclass AND attname = :c"
            ), {"c": column}).scalar()
            assert comment and comment.startswith("Cumulative")

    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert _columns(conn) == set()
    _flask_db(url, "upgrade")
