"""The request_logs_api_key_id revision on real PostgreSQL + TimescaleDB.

The test steps this module's database back to ``b4c6d8e0f2a4`` with the same
``flask db`` command the container runs, seeds a compressed chunk, upgrades
again, and checks what the revision relies on: the foreign key exists but was
added ``NOT VALID`` (so the upgrade never scanned the history), new rows are
still checked against ``api_keys``, and every older row reads NULL. It then
downgrades and checks the constraint and the column are gone.

A second test checks that the model and the migrated schema agree on the
column, its foreign key and its comment, so ``db.create_all`` and an upgraded
database build the same thing. It runs first, on the untouched upgrade.
"""

import os
import subprocess

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lumen.models.request_log import RequestLog

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

PREVIOUS = "b4c6d8e0f2a4"
SOURCE = "akmark"  # request_logs.source is VARCHAR(8)
FK = "fk_request_logs_api_key_id"


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


def _has_column(conn):
    return conn.execute(text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'request_logs' AND column_name = 'api_key_id'"
    )).scalar() == 1


def _constraint(conn):
    """(contype, convalidated, referenced table) of the FK, or None."""
    row = conn.execute(text(
        "SELECT contype, convalidated, confrelid::regclass::text FROM pg_constraint "
        "WHERE conname = :n AND conrelid = 'request_logs'::regclass"
    ), {"n": FK}).one_or_none()
    return tuple(row) if row else None


def _insert(conn, api_key_id):
    conn.execute(text(
        "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration, api_key_id) "
        "VALUES (now(), :s, 1, 1, 0.1, 0.1, :k)"
    ), {"s": SOURCE, "k": api_key_id})


def test_autogenerate_proposes_no_api_key_id_changes(pg_migrated_isolated):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), RequestLog.metadata)
    # Diffs come as tuples, or lists of tuples for column modifications.
    flat = [d for diff in diffs for d in (diff if isinstance(diff, list) else [diff])]
    assert [d for d in flat if "api_key_id" in repr(d) or FK in repr(d)] == []


def test_upgrade_adds_a_not_valid_fk_over_a_compressed_chunk(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert not _has_column(conn)

    # A row a month back lands in a closed 7-day chunk, which can be compressed.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (now() - INTERVAL '30 days', :s, 1, 1, 0.1, 0.1)"
        ), {"s": SOURCE})
        entity_id = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'api-key-id', 'AK', true) RETURNING id"
        )).scalar()
        key_id = conn.execute(text(
            "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
            " output_tokens, audio_seconds, cost, created_at)"
            " VALUES (:e, 'akmark', 'akmark', 0, 0, 0, 0, 0, now()) RETURNING id"
        ), {"e": entity_id}).scalar()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "SELECT compress_chunk(c, if_not_compressed => true) "
            "FROM show_chunks('request_logs', older_than => INTERVAL '7 days') c"
        ))
        compressed = conn.execute(text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND is_compressed"
        )).scalar()
    assert compressed >= 1, "no chunk was compressed, so this test would prove nothing"

    _flask_db(url, "upgrade")

    with engine.connect() as conn:
        assert _has_column(conn)
        # contype 'f' and convalidated false: the FK exists, and the upgrade
        # skipped the scan of existing rows that a validated FK would need.
        assert _constraint(conn) == ("f", False, "api_keys")
        comment = conn.execute(text(
            "SELECT col_description('request_logs'::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = 'request_logs'::regclass AND attname = 'api_key_id'"
        )).scalar()
        assert comment and comment.startswith("API key that made the request")

    # New rows are still checked, in a new chunk.
    unknown = key_id + 1_000_000
    with pytest.raises(IntegrityError, match=FK):
        with engine.begin() as conn:
            _insert(conn, unknown)
    with engine.begin() as conn:
        _insert(conn, key_id)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT api_key_id, count(*) FROM request_logs WHERE source = :s "
            "GROUP BY api_key_id ORDER BY api_key_id NULLS FIRST"
        ), {"s": SOURCE}).all()
        # The row from before the revision, in the compressed chunk, reads NULL.
        assert [tuple(r) for r in rows] == [(None, 1), (key_id, 1)]
        # The new rows went to the current chunk, not the compressed one.
        assert conn.execute(text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND is_compressed AND range_end > now()"
        )).scalar() == 0

    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert _constraint(conn) is None
        assert not _has_column(conn)
