"""Fixtures for tests that need a real PostgreSQL + TimescaleDB.

The rest of the suite runs on SQLite via ``db.create_all()`` (see the root
``conftest.py``), which never executes a line of Alembic and therefore never
creates the ``request_logs`` hypertable, its continuous aggregate, or any of the
``/api/usage/*`` SQL — those endpoints short-circuit on the dialect check before
reaching a query. Anything Timescale-shaped is untested without this.

Migrations run through ``flask db upgrade`` in a subprocess rather than through
Alembic's Python API, because that is verbatim what ``entrypoint.sh`` does in
production: the thing under test is the command the container actually runs.
"""

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

# Set by CI (see .github/workflows/test.yml). Absent locally unless a developer
# exports it, in which case these tests skip rather than fail.
PG_URL_ENV = "LUMEN_TEST_POSTGRES_URL"

TEST_CONFIG = str(Path(__file__).resolve().parents[1] / "fixtures" / "test_config.yaml")


def _admin_engine(url: str):
    # AUTOCOMMIT: CREATE DATABASE cannot run inside a transaction block.
    return create_engine(url, isolation_level="AUTOCOMMIT")


def flask_db(url: str, *args: str):
    """Run ``flask db <args>`` against ``url``, the way ``entrypoint.sh`` does.

    Exposed for tests that drive the migration chain themselves — running it
    only as far as a named revision, seeding, and then upgrading over the seed.
    """
    env = {**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG,
           "BACKGROUND_WORKER": "false"}
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"flask db {' '.join(args)} failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


@pytest.fixture
def pg_blank():
    """A created but deliberately *unmigrated* database: yields ``(url, engine)``.

    Every other fixture here hands back a database that is already at head,
    which cannot express "what happens to rows that were already in the table
    when a migration ran". Callers drive ``flask_db`` themselves.
    """
    base = os.environ.get(PG_URL_ENV)
    if not base:
        pytest.skip(f"{PG_URL_ENV} is not set; skipping PostgreSQL/TimescaleDB tests")

    name = f"lumen_blank_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    url = base.rsplit("/", 1)[0] + "/" + name

    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()
        admin = _admin_engine(base)
        with admin.connect() as conn:
            # Terminate stragglers first; DROP DATABASE fails while anything is connected.
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture(scope="session")
def pg_url():
    """A freshly created, disposable database on the CI PostgreSQL service."""
    base = os.environ.get(PG_URL_ENV)
    if not base:
        pytest.skip(f"{PG_URL_ENV} is not set; skipping PostgreSQL/TimescaleDB tests")

    # A per-run database so a failed run never poisons the next one, and so
    # these tests cannot touch anything a developer pointed the variable at.
    name = f"lumen_test_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()

    url = base.rsplit("/", 1)[0] + "/" + name
    yield url

    admin = _admin_engine(base)
    with admin.connect() as conn:
        # Terminate stragglers first; DROP DATABASE fails while anything is connected.
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
        ), {"n": name})
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def pg_migrated(pg_url):
    """Run the real migration chain against the disposable database.

    Returns an engine bound to it. The migration is the artifact under test, so
    a failure here is a test failure, not a fixture error — hence the explicit
    check on the return code with the output attached.
    """
    env = {**os.environ, "DATABASE_URL": pg_url}
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", "upgrade"],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"flask db upgrade failed against PostgreSQL:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def pg_migrated_isolated():
    """A migrated database private to the importing module: yields ``(url, engine)``.

    Deliberately *not* the session-scoped ``pg_migrated``. A continuous
    aggregate's watermark only ever moves forward, and
    ``refresh_continuous_aggregate(view, NULL, NULL)`` moves it to the end of the
    newest bucket that holds data — past the *current* bucket whenever a row
    inside it already exists. The Phase 8 modules do exactly that: they seed a
    current-bucket row and then refresh, in a downgrade/upgrade round trip and in
    a backfill. Once the watermark has moved, rows a later module writes into the
    current hour sit *below* it, real-time aggregation never scans them, and that
    module's equality assertions silently lose them. The same modules also add and
    remove retention policies and drop and recreate aggregates, all of which are
    global to the database.

    None of that is anything production does — the refresh policy's ``end_offset``
    is an hour and the backfill CLI clamps its window to ``now()`` — it is purely
    an artifact of one session sharing a database between modules, so the remedy
    is a private database rather than weaker assertions. It costs one extra
    ``flask db upgrade`` per module.
    """
    base = os.environ.get(PG_URL_ENV)
    if not base:
        pytest.skip(f"{PG_URL_ENV} is not set; skipping PostgreSQL/TimescaleDB tests")

    name = f"lumen_iso_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    url = base.rsplit("/", 1)[0] + "/" + name

    engine = None
    try:
        env = {**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG,
               "BACKGROUND_WORKER": "false"}
        result = subprocess.run(
            ["uv", "run", "flask", "--app", "run", "db", "upgrade"],
            capture_output=True, text=True, env=env, timeout=300,
        )
        assert result.returncode == 0, (
            f"flask db upgrade failed against PostgreSQL:\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        engine = create_engine(url)
        yield url, engine
    finally:
        # In a finally so a failed upgrade, or a test that errors out, still
        # drops the database instead of leaving it behind for the next run.
        if engine is not None:
            engine.dispose()
        admin = _admin_engine(base)
        with admin.connect() as conn:
            # Terminate stragglers first; DROP DATABASE fails while anything is connected.
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
