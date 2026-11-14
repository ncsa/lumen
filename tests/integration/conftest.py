"""Fixtures for tests that need a real PostgreSQL + TimescaleDB.

The rest of the suite runs on SQLite via ``db.create_all()`` (see the root
``conftest.py``), which never executes a line of Alembic and therefore never
creates the ``request_logs`` hypertable, its continuous aggregate, or any of the
``/api/usage/*`` SQL — those endpoints short-circuit on the dialect check before
reaching a query. Anything Timescale-shaped is untested without this.

Migrations run through ``flask db upgrade`` in a subprocess rather than through
Alembic's Python API, because that is verbatim what ``entrypoint.sh`` does in
production: the thing under test is the command the container actually runs.
Tests that step a database down and back up use ``migrate_db`` instead, which
runs the same Alembic commands in-process, as ``flask upgrade`` does, and so
skips a CLI start-up per step.
"""

import io
import logging
import os
import subprocess
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

import pytest
from alembic import command
from flask import current_app
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from lumen import create_app
from lumen.commands import STATS_WALK_LOCK_KEY
from lumen.extensions import db
from tests.conftest import no_snapshot_refresher

# Set by CI (see .github/workflows/test.yml). Absent locally unless a developer
# exports it, in which case these tests skip rather than fail.
PG_URL_ENV = "LUMEN_TEST_POSTGRES_URL"

TEST_CONFIG = str(Path(__file__).resolve().parents[1] / "fixtures" / "test_config.yaml")


# The loggers ``fileConfig`` in ``migrations/env.py`` configures from alembic.ini.
_MIGRATION_LOGGERS = ("", "sqlalchemy.engine", "alembic", "flask_migrate")


@contextmanager
def pg_app(url):
    """A Flask app bound to ``url``, built the way the ``flask`` CLI builds it.

    ``Config.SQLALCHEMY_DATABASE_URI`` is read from ``DATABASE_URL`` once, at
    import, so it is overridden for ``create_app`` and put back straight after.
    The environment stays set while the app is in use and is restored on exit.
    """
    names = ("DATABASE_URL", "CONFIG_YAML", "BACKGROUND_WORKER")
    previous_env = {k: os.environ.get(k) for k in names}
    os.environ.update({"DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"})
    # Not at the top: importing ``config`` at collection time would freeze its URI
    # before the session ``app`` fixture sets DATABASE_URL (``create_app`` defers
    # the same import for the same reason).
    from config import Config

    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    try:
        with pytest.MonkeyPatch.context() as mp:
            no_snapshot_refresher(mp)
            application = create_app()
        Config.SQLALCHEMY_DATABASE_URI = previous_uri
        yield application
    finally:
        Config.SQLALCHEMY_DATABASE_URI = previous_uri
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def migrate_db(url, direction, revision, sql=False):
    """Run ``flask db upgrade|downgrade <revision> [--sql]`` against ``url`` in-process.

    Calls Alembic with the config Flask-Migrate builds, which is what
    ``flask_migrate.upgrade``/``downgrade`` do, minus their wrapper that turns
    a failure into ``sys.exit(1)``: a failing migration raises its own error.
    With ``sql=True`` the migration runs offline and the emitted SQL, which
    Alembic writes to stdout, is returned.

    ``migrations/env.py`` calls ``fileConfig``, which replaces the root
    logger's handlers, so the loggers it touches are restored afterwards; the
    app's engine is disposed so no connection outlives the call.
    """
    run = {"upgrade": command.upgrade, "downgrade": command.downgrade}[direction]
    saved = [
        (lg, lg.handlers[:], lg.level, lg.propagate, lg.disabled)
        for lg in map(logging.getLogger, _MIGRATION_LOGGERS)
    ]
    try:
        with pg_app(url) as app, app.app_context():
            try:
                config = current_app.extensions["migrate"].migrate.get_config()
                if not sql:
                    run(config, revision)
                    return None
                out = io.StringIO()
                with redirect_stdout(out):
                    run(config, revision, sql=True)
                return out.getvalue()
            finally:
                db.engine.dispose()
    finally:
        for lg, handlers, level, propagate, disabled in saved:
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate
            lg.disabled = disabled


def _admin_engine(url: str):
    # AUTOCOMMIT: CREATE DATABASE cannot run inside a transaction block.
    return create_engine(url, isolation_level="AUTOCOMMIT")


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


@pytest.fixture(scope="session")
def _pg_template():
    """A database migrated once per session, for ``pg_migrated_isolated`` to copy.

    ``CREATE DATABASE ... TEMPLATE`` refuses while anything is connected to the
    source, and TimescaleDB's background scheduler connects to every database
    with the extension, so connections are disallowed before the stragglers are
    terminated.
    """
    base = os.environ.get(PG_URL_ENV)
    if not base:
        pytest.skip(f"{PG_URL_ENV} is not set; skipping PostgreSQL/TimescaleDB tests")

    name = f"lumen_tmpl_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    url = base.rsplit("/", 1)[0] + "/" + name

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
        admin = _admin_engine(base)
        with admin.connect() as conn:
            conn.execute(text(f'ALTER DATABASE "{name}" WITH ALLOW_CONNECTIONS false'))
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
        admin.dispose()
        yield name
    finally:
        admin = _admin_engine(base)
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture(scope="module")
def pg_migrated_isolated(_pg_template):
    """A migrated database private to the importing module: yields ``(url, engine)``.

    Deliberately *not* the session-scoped ``pg_migrated``. A continuous
    aggregate's watermark only ever moves forward, and
    ``refresh_continuous_aggregate(view, NULL, NULL)`` moves it to the end of the
    newest bucket that holds data — past the *current* bucket whenever a row
    inside it already exists. The Phase 8 modules do exactly that: they seed a
    current-bucket row and then refresh in a backfill. Once the watermark has
    moved, rows a later module writes into the current hour sit *below* it,
    real-time aggregation never scans them, and that module's equality
    assertions silently lose them. The same modules also add and remove
    retention policies and drop and recreate aggregates, all of which are
    global to the database.

    None of that is anything production does — the refresh policy's ``end_offset``
    is an hour and the backfill CLI clamps its window to ``now()`` — it is purely
    an artifact of one session sharing a database between modules, so the remedy
    is a private database rather than weaker assertions. Each one is a copy of
    ``_pg_template``, which ran the real ``flask db upgrade`` once.
    """
    base = os.environ[PG_URL_ENV]
    name = f"lumen_iso_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"'))
    admin.dispose()
    url = base.rsplit("/", 1)[0] + "/" + name

    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        # In a finally so a test that errors out still drops the database
        # instead of leaving it behind for the next run.
        engine.dispose()
        admin = _admin_engine(base)
        with admin.connect() as conn:
            # Terminate stragglers first; DROP DATABASE fails while anything is connected.
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@contextmanager
def holding_stats_walk_lock(engine):
    """Hold the advisory lock reassign-model, erase-entity and check-stats share,
    on a connection of its own, as another of those commands would."""
    with engine.connect() as conn:
        assert conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": STATS_WALK_LOCK_KEY}).scalar()
        conn.commit()
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": STATS_WALK_LOCK_KEY})
            conn.commit()


def stats_walk_lock_is_free(engine):
    """Whether no session holds the shared advisory lock: takes it and gives it back.

    Probes on a new physical session, never one from ``engine``'s pool: a pooled
    session may be the one holding a leaked lock, and advisory locks are reentrant
    for their owner, so it would take the lock again and report it free.
    """
    probe = create_engine(engine.url, poolclass=NullPool)
    try:
        with probe.connect() as conn:
            free = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": STATS_WALK_LOCK_KEY}).scalar()
            if free:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": STATS_WALK_LOCK_KEY})
            conn.commit()
            return free
    finally:
        probe.dispose()
