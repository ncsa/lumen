"""The TimescaleDB health check and its startup log (#173), with mocked catalog rows.

The PostgreSQL side, against a real migrated database, is in
``tests/integration/test_timescale_health_postgres.py``.
"""
import logging
from datetime import datetime

import pytest
from sqlalchemy.exc import ProgrammingError

from lumen import _log_timescale_health
from lumen.extensions import db
from lumen.services import timescale_health
from lumen.services.timescale_health import (
    EXTENSION_MISSING,
    HYPERTABLE_MISSING,
    REQUEST_LOGS_RETENTION,
    TimescaleHealth,
    check,
    problems,
)

_FINISHED = datetime(2026, 10, 30, 5, 0)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Conn:
    """Answers each statement with the next canned row set and records the statements."""

    def __init__(self, *row_sets):
        self._row_sets = list(row_sets)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _Result(self._row_sets.pop(0))


def _aggregates(**overrides):
    rows = {
        "request_counts_hourly": ("request_counts_hourly", False, "_materialized_hypertable_3"),
        "request_counts_hourly_by_entity":
            ("request_counts_hourly_by_entity", False, "_materialized_hypertable_5"),
        "request_metrics_1m": ("request_metrics_1m", False, "_materialized_hypertable_7"),
    }
    rows.update(overrides)
    return [r for r in rows.values() if r is not None]


def _jobs(*extra, drop=()):
    """The jobs a fresh migration creates; cagg policies reported against the materialisation table."""
    rows = [
        (1, "policy_telemetry", None, "Failed", 4, None),
        (1000, "policy_refresh_continuous_aggregate", "_materialized_hypertable_3", "Success", 0, _FINISHED),
        (1001, "policy_compression", "request_logs", "Success", 0, _FINISHED),
        (1002, "policy_refresh_continuous_aggregate", "_materialized_hypertable_5", "Success", 0, _FINISHED),
        (1003, "policy_refresh_continuous_aggregate", "_materialized_hypertable_7", "Success", 0, _FINISHED),
        (1004, "policy_retention", "request_metrics_1m", "Success", 0, _FINISHED),
    ]
    return [r for r in rows if r[0] not in drop] + list(extra)


def _healthy_conn(aggregates=None, jobs=None, version=("2.27.2", "2.27.2"), hypertable=True):
    return _Conn(
        [version],
        [(hypertable,)],
        _aggregates() if aggregates is None else aggregates,
        _jobs() if jobs is None else jobs,
    )


def test_fresh_migration_has_no_problems():
    conn = _healthy_conn()
    health = check(conn)
    assert problems(health) == []
    assert len(conn.statements) == 4
    assert [name for name, _ in health.jobs] == list(timescale_health.EXPECTED_JOBS)
    assert health.job("retention:request_metrics_1m").job_id == 1004
    assert health.job("refresh:request_metrics_1m").target == "request_metrics_1m"
    assert health.request_logs_retention is False


def test_cagg_jobs_reported_against_the_view_also_match():
    view_of = {mat: view for view, _, mat in _aggregates()}
    jobs = [(i, proc, view_of.get(t, t), s, f, last) for (i, proc, t, s, f, last) in _jobs()]
    assert problems(check(_healthy_conn(jobs=jobs))) == []


def test_statements_are_read_only():
    conn = _healthy_conn()
    check(conn)
    for statement in conn.statements:
        assert statement.lstrip().upper().startswith("SELECT"), statement


def test_missing_extension_runs_one_statement_and_reports_only_that():
    conn = _Conn([(None, "2.27.2")])
    health = check(conn)
    assert len(conn.statements) == 1
    assert problems(health) == [EXTENSION_MISSING]
    assert "TimescaleDB is required" in EXTENSION_MISSING


def test_extension_not_available_at_all_is_missing():
    assert problems(check(_Conn([]))) == [EXTENSION_MISSING]


def test_extension_update_available():
    found = problems(check(_healthy_conn(version=("2.26.4", "2.27.2"))))
    assert len(found) == 1
    assert "2.26.4" in found[0] and "2.27.2" in found[0]
    assert "ALTER EXTENSION timescaledb UPDATE" in found[0]


def test_request_logs_not_a_hypertable():
    assert problems(check(_healthy_conn(hypertable=False))) == [HYPERTABLE_MISSING]


def test_missing_aggregate_also_loses_its_refresh_job():
    found = problems(check(_healthy_conn(
        aggregates=_aggregates(request_counts_hourly=None), jobs=_jobs(drop=(1000,)))))
    assert found == [
        "Continuous aggregate request_counts_hourly is missing.",
        "Background job refresh:request_counts_hourly "
        + "(policy_refresh_continuous_aggregate on request_counts_hourly) is missing.",
    ]


def test_materialized_only_aggregate():
    found = problems(check(_healthy_conn(aggregates=_aggregates(
        request_metrics_1m=("request_metrics_1m", True, "_materialized_hypertable_7")))))
    assert len(found) == 1
    assert "request_metrics_1m has materialized_only = true" in found[0]


@pytest.mark.parametrize("job_id, name", [
    (1000, "refresh:request_counts_hourly"),
    (1001, "compression:request_logs"),
    (1004, "retention:request_metrics_1m"),
])
def test_missing_job(job_id, name):
    found = problems(check(_healthy_conn(jobs=_jobs(drop=(job_id,)))))
    assert len(found) == 1
    assert found[0].startswith(f"Background job {name} ")
    assert found[0].endswith("is missing.")


def test_failed_expected_job():
    jobs = [r if r[0] != 1001 else (1001, "policy_compression", "request_logs", "Failed", 3, _FINISHED)
            for r in _jobs()]
    assert problems(check(_healthy_conn(jobs=jobs))) == [
        "Background job compression:request_logs (job 1001) failed its last run; 3 failures in total."
    ]


def test_failed_unrelated_job_is_ignored():
    """The telemetry job fails on every air-gapped install; it is not Lumen's."""
    health = check(_healthy_conn())
    assert problems(health) == []


def test_unexpected_catalog_shape_reports_missing_jobs_without_crashing():
    jobs = [(i, proc, None, None, None, None) for (i, proc, *_rest) in _jobs()]
    found = problems(check(_healthy_conn(jobs=jobs)))
    assert len(found) == len(timescale_health.EXPECTED_JOBS)
    assert all(p.endswith("is missing.") for p in found)


def test_request_logs_retention_is_its_own_flag():
    jobs = _jobs((1005, "policy_retention", "request_logs", "Success", 0, _FINISHED))
    health = check(_healthy_conn(jobs=jobs))
    assert health.request_logs_retention is True
    assert problems(health) == [REQUEST_LOGS_RETENTION]
    assert problems(health, include_request_logs_retention=False) == []


def test_request_metrics_1m_retention_never_sets_the_request_logs_flag():
    health = check(_healthy_conn())
    assert health.job("retention:request_metrics_1m") is not None
    assert health.request_logs_retention is False


def test_health_is_frozen():
    health = TimescaleHealth(installed_version="2.27.2", available_version="2.27.2")
    with pytest.raises(AttributeError):
        health.installed_version = "x"


# --- startup log -------------------------------------------------------------------


class _PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def _logged(caplog):
    """INFO and above from the app's logger; other loggers are not under test."""
    return [r for r in caplog.records if r.name == "lumen" and r.levelno >= logging.INFO]


def _as_postgres(monkeypatch):
    monkeypatch.setattr(db.engine.dialect, "name", "postgresql")


def _with_health(monkeypatch, conn):
    monkeypatch.setattr(timescale_health, "check", lambda session: check(conn))


def test_sqlite_startup_runs_no_health_check(app, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(timescale_health, "check", lambda session: calls.append(session))
    with app.app_context(), caplog.at_level(logging.INFO):
        _log_timescale_health(app)
    assert calls == []
    assert _logged(caplog) == []


def test_healthy_database_logs_one_info_summary(app, monkeypatch, caplog):
    _with_health(monkeypatch, _healthy_conn())
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.INFO]
    assert "TimescaleDB 2.27.2" in records[0].getMessage()
    assert "5 expected jobs" in records[0].getMessage()


def test_missing_extension_logs_an_error(app, monkeypatch, caplog):
    _with_health(monkeypatch, _Conn([(None, "2.27.2")]))
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert "TimescaleDB is required" in records[0].getMessage()


def test_missing_hypertable_is_an_error_and_other_problems_are_warnings(app, monkeypatch, caplog):
    _with_health(monkeypatch, _healthy_conn(hypertable=False, jobs=_jobs(drop=(1001,))))
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.ERROR, logging.WARNING]
    assert "request_logs is not a TimescaleDB hypertable" in records[0].getMessage()
    assert "compression:request_logs" in records[1].getMessage()


def test_request_logs_retention_is_left_to_the_tripwire(app, monkeypatch, caplog):
    """Not logged again, and no "healthy" summary that would contradict the tripwire's ERROR."""
    jobs = _jobs((1005, "policy_retention", "request_logs", "Success", 0, _FINISHED))
    _with_health(monkeypatch, _healthy_conn(jobs=jobs))
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
    assert _logged(caplog) == []


def test_missing_catalog_is_skipped_quietly(app, monkeypatch, caplog):
    def raise_undefined_table(session):
        raise ProgrammingError("SELECT ...", {}, _PgError("42P01"))

    monkeypatch.setattr(timescale_health, "check", raise_undefined_table)
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
    assert _logged(caplog) == []


def test_query_failure_is_a_warning(app, monkeypatch, caplog):
    def raise_other_error(session):
        raise ProgrammingError("SELECT ...", {}, _PgError("42501"))

    monkeypatch.setattr(timescale_health, "check", raise_other_error)
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "Could not check TimescaleDB health" in records[0].getMessage()


def test_failure_leaves_the_session_usable(app, monkeypatch, caplog):
    """A failed flush leaves the session pending-rollback; the check must roll it back."""
    from lumen.models.model_config import ModelConfig

    def failing_flush(session):
        # Missing required cost fields: the flush raises IntegrityError.
        db.session.add(ModelConfig(model_name="health-flush-failure"))
        db.session.flush()

    monkeypatch.setattr(timescale_health, "check", failing_flush)
    with app.app_context(), caplog.at_level(logging.INFO):
        _as_postgres(monkeypatch)
        _log_timescale_health(app)
        assert db.session.execute(db.text("SELECT 1")).scalar() == 1
    assert [r.levelno for r in _logged(caplog)] == [logging.WARNING]
