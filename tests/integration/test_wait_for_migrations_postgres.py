"""``flask wait-for-migrations`` against real PostgreSQL, run as the container runs it.

The SQLite tests in ``tests/unit/test_wait_for_migrations.py`` cover the waiting
logic with a fake clock; these prove the command reads the head the real migration
chain leaves behind, and that an empty database holds the gate shut. The command is
read-only, so the shared migrated database is safe to use.
"""

import os
import subprocess
import uuid

import pytest
from sqlalchemy import text

from tests.integration.conftest import PG_URL_ENV, TEST_CONFIG, _admin_engine
from tests.unit.test_migrations import _script_dir

pytestmark = pytest.mark.postgres


def _run(url, *args):
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "CONFIG_YAML": TEST_CONFIG,
        "BACKGROUND_WORKER": "false",
    }
    return subprocess.run(
        ["uv", "run", "flask", "--app", "run", "wait-for-migrations", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


@pytest.fixture
def empty_pg_url():
    """A database that exists but was never migrated."""
    base = os.environ.get(PG_URL_ENV)
    if not base:
        pytest.skip(f"{PG_URL_ENV} is not set; skipping PostgreSQL/TimescaleDB tests")
    name = f"lumen_empty_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    try:
        yield base.rsplit("/", 1)[0] + "/" + name
    finally:
        admin = _admin_engine(base)
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_passes_on_migrated_database(pg_url, pg_migrated):
    result = _run(pg_url, "--timeout", "30")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    with pg_migrated.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert f"Database is at migration head {head}." in result.stdout


def test_waits_on_empty_database(empty_pg_url):
    result = _run(empty_pg_url, "--timeout", "3")
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "Waiting for migrations: database is at revision none" in result.stdout
    assert "timed out after 3s" in result.stdout
    # Read-only: waiting never creates alembic_version, let alone migrates.
    engine = _admin_engine(empty_pg_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('alembic_version')")).scalar() is None
    engine.dispose()


def test_reads_the_schema_the_url_selects(empty_pg_url):
    """``?options=-c search_path=...`` must reach the gate's connection.

    Only the ``tenant`` schema is stamped at head; ``public`` has no alembic_version.
    A gate that dropped the URL's options for its own statement timeout would read
    ``public`` and wait on a database that is already migrated.
    """
    head = _script_dir().get_current_head()
    engine = _admin_engine(empty_pg_url)
    with engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA tenant"))
        conn.execute(text("CREATE TABLE tenant.alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO tenant.alembic_version VALUES (:r)"), {"r": head})
    engine.dispose()

    result = _run(empty_pg_url + "?options=-c%20search_path%3Dtenant", "--timeout", "3")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"Database is at migration head {head}." in result.stdout

    # Without the option the same database is unstamped: the test above is not vacuous.
    result = _run(empty_pg_url, "--timeout", "1")
    assert result.returncode == 1
    assert "database is at revision none" in result.stdout
