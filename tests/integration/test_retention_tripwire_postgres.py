"""The startup tripwire for a retention policy on ``request_logs``, on real TimescaleDB.

``request_logs`` is never pruned (#167). A policy added by hand in SQL must be
logged as an ERROR when a serving process starts, with exactly one catalog query
and without stopping startup. A retention policy is global to the database, so
this module runs against ``pg_migrated_isolated``.
"""
import logging

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_engine(pg_migrated_isolated):
    engine = pg_migrated_isolated[1]
    yield engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT remove_retention_policy('request_logs', if_exists => true)"))


@pytest.fixture
def create_pg_app(pg_migrated_isolated, monkeypatch):
    """``create_app`` bound to the isolated database, as a serving process.

    ``config.Config`` reads DATABASE_URL at class-definition time and the root
    conftest has already imported it, so the URI is patched on the class too.
    """
    url = pg_migrated_isolated[0]
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CONFIG_YAML", TEST_CONFIG)
    monkeypatch.setenv("BACKGROUND_WORKER", "false")
    from config import Config
    monkeypatch.setattr(Config, "SQLALCHEMY_DATABASE_URI", url)
    from lumen.services import metrics_snapshot
    monkeypatch.setattr(metrics_snapshot, "start_snapshot_refresher", lambda app: None)

    created = []

    def _create():
        from lumen import create_app
        app = create_app()
        created.append(app)
        return app

    yield _create
    from lumen.extensions import db
    for app in created:
        with app.app_context():
            db.engine.dispose()


def _logged(caplog):
    """The tripwire's own records; other startup warnings are not under test."""
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING and "retention policy" in r.getMessage()]


def test_retention_policy_on_request_logs_logs_an_error(pg_engine, create_pg_app, caplog):
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))

    with caplog.at_level(logging.WARNING):
        app = create_pg_app()

    assert app is not None, "startup must succeed with a retention policy present"
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.ERROR], [r.getMessage() for r in records]
    msg = records[0].getMessage()
    assert "request_logs has a retention policy" in msg
    assert "must never be pruned" in msg
    assert "SELECT remove_retention_policy('request_logs');" in msg
    assert "flask check-aggregate" in msg


def test_no_retention_policy_logs_nothing_with_one_query(pg_engine, create_pg_app, caplog):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        with caplog.at_level(logging.WARNING):
            create_pg_app()
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert _logged(caplog) == [], [r.getMessage() for r in _logged(caplog)]
    assert sum("policy_retention" in s for s in statements) == 1, statements


def test_retention_policy_on_request_metrics_1m_logs_nothing(pg_engine, create_pg_app, caplog):
    """The migration chain keeps 30 days of request_metrics_1m (#169); that is not request_logs."""
    with pg_engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.jobs j "
            "JOIN timescaledb_information.continuous_aggregates c "
            "  ON c.materialization_hypertable_name = j.hypertable_name "
            "WHERE j.proc_name = 'policy_retention' AND c.view_name = 'request_metrics_1m'"
        )).scalar() == 1, "the policy must exist or this test proves nothing"

    with caplog.at_level(logging.WARNING):
        create_pg_app()

    assert _logged(caplog) == [], [r.getMessage() for r in _logged(caplog)]


def test_aborted_lookup_transaction_is_rolled_back(pg_engine, create_pg_app, monkeypatch, caplog):
    """A failed lookup aborts the PostgreSQL transaction; the tripwire must roll it back.

    Without the rollback every later statement in the app context fails with
    "current transaction is aborted", which is what the startup steps after the
    tripwire (metrics priming) would hit.
    """
    app = create_pg_app()

    def aborting_lookup(conn):
        conn.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr("lumen.commands._retention_job", aborting_lookup)
    from lumen import _warn_if_request_logs_has_retention
    from lumen.extensions import db
    with app.app_context(), caplog.at_level(logging.WARNING):
        _warn_if_request_logs_has_retention(app)
        assert db.session.execute(text("SELECT 1")).scalar() == 1
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.WARNING], [r.getMessage() for r in records]
    assert "Could not check request_logs" in records[0].getMessage()
