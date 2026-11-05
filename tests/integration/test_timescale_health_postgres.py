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
    assert health.job("refresh:request_counts_daily").target == "request_counts_daily"
    assert health.aggregate("request_counts_daily").materialized_only is False
    assert health.job("refresh:request_counts_hourly_total").target == "request_counts_hourly_total"
    assert health.aggregate("request_counts_hourly_total").materialized_only is False
    assert health.job("refresh:request_counts_daily_by_entity").target \
        == "request_counts_daily_by_entity"
    assert health.aggregate("request_counts_daily_by_entity").materialized_only is False
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
    monkeypatch.setattr(metrics_snapshot, "start_snapshot_refresher", lambda app, **kwargs: None)

    from lumen import create_app
    from lumen.extensions import db
    with caplog.at_level(logging.INFO):
        app = create_app()
    try:
        records = [r for r in caplog.records if "TimescaleDB" in r.getMessage()]
        assert [r.levelno for r in records] == [logging.INFO], [r.getMessage() for r in records]
        assert "7 expected jobs" in records[0].getMessage()
    finally:
        with app.app_context():
            db.engine.dispose()


def test_job_that_never_succeeded_is_reported_as_failed(conn):
    """A job that ran but never succeeded keeps ``last_successful_finish = '-infinity'`` (#417).

    NOTE: this writes TimescaleDB's internal ``_timescaledb_internal.bgw_job_stat``
    table directly, and its columns are pinned to the CI image
    (``timescale/timescaledb:2.27.2-pg17``). If a TimescaleDB upgrade breaks this
    test, the failure points here: switch to the scheduler-driven approach of
    ``tests/integration/test_timescale_job_failure_postgres.py`` (an isolated
    database, deleting the job's stat row first so the first run is the failing one).
    """
    job_id = check(conn).job("refresh:request_counts_hourly").job_id
    # last_start/last_finish/next_start must be real timestamps: job_stats
    # reports last_run_status = NULL when last_finish is -infinity.
    conn.execute(text(
        "INSERT INTO _timescaledb_internal.bgw_job_stat ("
        " job_id, last_start, last_finish, next_start, last_successful_finish,"
        " last_run_success, total_runs, total_duration, total_duration_failures,"
        " total_successes, total_failures, total_crashes, consecutive_failures,"
        " consecutive_crashes"
        ") VALUES ("
        " :job_id, now(), now(), now(), '-infinity',"
        " false, 1, interval '0', interval '0',"
        " 0, 1, 0, 0,"
        " 0"
        ") ON CONFLICT (job_id) DO UPDATE SET"
        " last_start = EXCLUDED.last_start, last_finish = EXCLUDED.last_finish,"
        " next_start = EXCLUDED.next_start,"
        " last_successful_finish = EXCLUDED.last_successful_finish,"
        " last_run_success = EXCLUDED.last_run_success,"
        " total_runs = EXCLUDED.total_runs, total_duration = EXCLUDED.total_duration,"
        " total_duration_failures = EXCLUDED.total_duration_failures,"
        " total_successes = EXCLUDED.total_successes,"
        " total_failures = EXCLUDED.total_failures,"
        " total_crashes = EXCLUDED.total_crashes,"
        " consecutive_failures = EXCLUDED.consecutive_failures,"
        " consecutive_crashes = EXCLUDED.consecutive_crashes"
    ), {"job_id": job_id})

    health = check(conn)

    job = health.job("refresh:request_counts_hourly")
    assert job.last_run_status == "Failed"
    assert job.last_successful_finish is None
    assert problems(health) == [
        f"Background job refresh:request_counts_hourly (job {job_id}) failed its last run; "
        "1 failures in total."
    ]
