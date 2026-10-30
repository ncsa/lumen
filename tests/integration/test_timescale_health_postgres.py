"""The TimescaleDB health check (#173), against a real migrated database.

Each breakage is made inside a transaction that is rolled back, so the module
shares one ``pg_migrated_isolated`` database without the tests seeing each
other's changes.
"""
import logging

import pytest
from sqlalchemy import event, text

from lumen.commands import _retention_job
from lumen.services import timescale_health
from lumen.services.timescale_health import REQUEST_LOGS_RETENTION, check, problems
from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_engine(pg_migrated_isolated):
    return pg_migrated_isolated[1]


@pytest.fixture
def conn(pg_engine):
    """A connection whose transaction is always rolled back."""
    with pg_engine.connect() as c:
        try:
            yield c
        finally:
            c.rollback()


def test_fresh_migration_has_no_problems(conn):
    statements = []

    def record(c, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(conn, "before_cursor_execute", record)
    try:
        health = check(conn)
    finally:
        event.remove(conn, "before_cursor_execute", record)

    assert problems(health) == []
    assert len(statements) <= 4, statements
    assert health.installed_version == health.available_version
    assert health.request_logs_hypertable is True
    assert [name for name, _ in health.jobs] == list(timescale_health.EXPECTED_JOBS)
    assert health.job("retention:request_metrics_1m").proc_name == "policy_retention"
    assert health.request_logs_retention is False


def test_removed_refresh_policy_is_a_missing_job(conn):
    conn.execute(text("SELECT remove_continuous_aggregate_policy('request_counts_hourly')"))
    found = problems(check(conn))
    assert len(found) == 1, found
    assert found[0].startswith("Background job refresh:request_counts_hourly ")
    assert found[0].endswith("is missing.")


def test_materialized_only_aggregate_is_reported(conn):
    conn.execute(text(
        "ALTER MATERIALIZED VIEW request_metrics_1m SET (timescaledb.materialized_only = true)"
    ))
    found = problems(check(conn))
    assert len(found) == 1, found
    assert "request_metrics_1m has materialized_only = true" in found[0]


def test_request_logs_retention_is_reported_and_agrees_with_the_tripwire(conn):
    conn.execute(text(
        "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
    ))
    health = check(conn)
    assert health.request_logs_retention is True
    assert _retention_job(conn) is not None
    assert problems(health) == [REQUEST_LOGS_RETENTION]


def test_request_metrics_1m_retention_never_sets_the_request_logs_flag(conn):
    assert conn.execute(text(
        "SELECT COUNT(*) FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_metrics_1m'"
    )).scalar() == 1, "the policy must exist or this test proves nothing"
    health = check(conn)
    assert health.job("retention:request_metrics_1m") is not None
    assert health.request_logs_retention is False
    assert _retention_job(conn) is None


def test_startup_logs_no_problem_on_a_fresh_database(pg_migrated_isolated, monkeypatch, caplog):
    """``create_app`` as a serving process: the summary is INFO only, and startup succeeds."""
    url = pg_migrated_isolated[0]
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CONFIG_YAML", TEST_CONFIG)
    monkeypatch.setenv("BACKGROUND_WORKER", "false")
    from config import Config
    monkeypatch.setattr(Config, "SQLALCHEMY_DATABASE_URI", url)
    from lumen.services import metrics_snapshot
    monkeypatch.setattr(metrics_snapshot, "start_snapshot_refresher", lambda app: None)

    from lumen import create_app
    from lumen.extensions import db
    with caplog.at_level(logging.INFO):
        app = create_app()
    try:
        records = [r for r in caplog.records if "TimescaleDB" in r.getMessage()]
        assert [r.levelno for r in records] == [logging.INFO], [r.getMessage() for r in records]
        assert "5 expected jobs" in records[0].getMessage()
    finally:
        with app.app_context():
            db.engine.dispose()
