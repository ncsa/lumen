"""The stats_deferred_marker revision on real PostgreSQL + TimescaleDB.

Each test steps this module's database back to ``e4f5a6b7c8d9`` with the same
``flask db`` command the container runs, sets up the state the revision meets
in production, and upgrades again:

1. A cutover someone already set is kept, and the downgrade puts PostgreSQL
   back to "not cut over" and drops the column.
2. The column is added to a ``request_logs`` that has a compressed chunk, and
   rows can still be written afterwards, into that chunk and into a new one.

The automatic cutover on a fresh chain is asserted in
``tests/integration/test_migrations_postgres.py``. The tests run in file order:
the second leaves a compressed chunk behind, so it goes last.
"""

import os
import subprocess
from datetime import datetime

import pytest
from sqlalchemy import text

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

PREVIOUS = "e4f5a6b7c8d9"
SOURCE = "sdmark"  # request_logs.source is VARCHAR(8)


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
        "WHERE table_name = 'request_logs' AND column_name = 'stats_deferred'"
    )).scalar() == 1


def _fold_state(conn):
    return tuple(conn.execute(text("SELECT cutover_at, folded_until FROM stats_fold_state WHERE id = 1")).one())


def test_downgrade_resets_the_cutover_and_upgrade_keeps_an_existing_one(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert not _has_column(conn)
        assert _fold_state(conn) == (None, None)

    existing = datetime(2026, 10, 1, 12, 0, 0)
    watermark = datetime(2026, 10, 1, 12, 30, 0)
    with engine.begin() as conn:
        conn.execute(text("UPDATE stats_fold_state SET cutover_at = :c, folded_until = :f WHERE id = 1"),
                     {"c": existing, "f": watermark})

    _flask_db(url, "upgrade")
    with engine.connect() as conn:
        assert _has_column(conn)
        assert _fold_state(conn) == (existing, watermark)


def test_upgrade_adds_the_column_over_a_compressed_chunk(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    _flask_db(url, "downgrade", PREVIOUS)

    # A row a month back lands in a closed 7-day chunk, which can be compressed.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (now() - INTERVAL '30 days', :s, 1, 1, 0.1, 0.1)"
        ), {"s": SOURCE})
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

    with engine.begin() as conn:
        assert _has_column(conn)
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration, stats_deferred) "
            "VALUES (now() - INTERVAL '30 days', :s, 1, 1, 0.1, 0.1, TRUE), "
            "       (now(), :s, 1, 1, 0.1, 0.1, TRUE)"
        ), {"s": SOURCE})
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT stats_deferred, count(*) FROM request_logs WHERE source = :s "
            "GROUP BY stats_deferred ORDER BY stats_deferred NULLS FIRST"
        ), {"s": SOURCE}).all()
    # The row from before the revision reads NULL: billing counted it directly.
    assert [tuple(r) for r in rows] == [(None, 1), (True, 2)]
