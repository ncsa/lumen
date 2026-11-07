"""``b8c0d2e4f6a1`` converts drifted ``timestamptz`` columns to naive UTC.

Databases that predate Alembic carry some columns as ``timestamptz``. The module
makes two of them aware again at the previous revision, upgrades, and checks the
type is naive with the instant preserved. It leaves the database at head.
"""
import os
import subprocess
from datetime import datetime

import pytest
from sqlalchemy import text

from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

_BEFORE = "f9b1d3e5a7c9"
_REVISION = "b8c0d2e4f6a1"


def _flask_db(url, *args):
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"},
    )
    assert result.returncode == 0, (
        f"flask db {' '.join(args)} failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _type(conn, table, column):
    return conn.execute(text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"), {"t": table, "c": column}).scalar()


def test_upgrade_makes_aware_columns_naive_utc(pg_migrated_isolated):
    url, engine = pg_migrated_isolated

    _flask_db(url, "downgrade", _BEFORE)
    engine.dispose()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE model_stats ALTER COLUMN last_used_at TYPE timestamptz USING last_used_at AT TIME ZONE 'UTC'"))
        conn.execute(text("ALTER TABLE entities ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC'"))
        conn.execute(text(
            "INSERT INTO entities (entity_type, email, name, initials, active, store_conversations, created_at) "
            "VALUES ('user', 'naive@test', 'N', 'N', true, true, '2026-01-02 03:04:05+00')"))
    engine.dispose()

    _flask_db(url, "upgrade", _REVISION)
    engine.dispose()
    with engine.connect() as conn:
        assert _type(conn, "model_stats", "last_used_at") == "timestamp without time zone"
        assert _type(conn, "entities", "created_at") == "timestamp without time zone"
        assert conn.execute(text("SELECT created_at FROM entities WHERE email = 'naive@test'")).scalar() == datetime(2026, 1, 2, 3, 4, 5)
    engine.dispose()
