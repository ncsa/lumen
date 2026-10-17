"""The startup tripwire for a retention policy on ``request_logs`` (#167).

``request_logs`` is never pruned, so a policy added by hand in SQL must be loud
at startup — but the check must never stop the app from starting. These tests
run on SQLite; the PostgreSQL side is in
``tests/integration/test_retention_tripwire_postgres.py``.
"""
import logging

from sqlalchemy.exc import ProgrammingError

from lumen import _warn_if_request_logs_has_retention
from lumen.extensions import db


class _PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def _logged(caplog):
    """WARNING and above from the app's logger; other loggers are not under test."""
    return [r for r in caplog.records if r.name == "lumen" and r.levelno >= logging.WARNING]


def _as_postgres(monkeypatch):
    monkeypatch.setattr(db.engine.dialect, "name", "postgresql")


def test_sqlite_startup_runs_no_retention_query(app, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr("lumen.commands._retention_job", lambda conn: calls.append(conn))
    with app.app_context(), caplog.at_level(logging.WARNING):
        _warn_if_request_logs_has_retention(app)
    assert calls == []
    assert _logged(caplog) == []


def test_policy_present_logs_an_error(app, monkeypatch, caplog):
    monkeypatch.setattr("lumen.commands._retention_job",
                        lambda conn: (1000, '{"drop_after": "13 mons"}'))
    with app.app_context(), caplog.at_level(logging.WARNING):
        _as_postgres(monkeypatch)
        _warn_if_request_logs_has_retention(app)
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.ERROR]
    msg = records[0].getMessage()
    assert "job 1000" in msg
    assert "must never be pruned" in msg
    assert "SELECT remove_retention_policy('request_logs');" in msg
    assert "flask check-aggregate" in msg


def test_no_policy_logs_nothing(app, monkeypatch, caplog):
    monkeypatch.setattr("lumen.commands._retention_job", lambda conn: None)
    with app.app_context(), caplog.at_level(logging.WARNING):
        _as_postgres(monkeypatch)
        _warn_if_request_logs_has_retention(app)
    assert _logged(caplog) == []


def test_missing_timescaledb_catalog_is_skipped_quietly(app, monkeypatch, caplog):
    def raise_undefined_table(conn):
        raise ProgrammingError("SELECT ...", {}, _PgError("42P01"))

    monkeypatch.setattr("lumen.commands._retention_job", raise_undefined_table)
    with app.app_context(), caplog.at_level(logging.WARNING):
        _as_postgres(monkeypatch)
        _warn_if_request_logs_has_retention(app)
    assert _logged(caplog) == []


def test_query_failure_is_a_warning(app, monkeypatch, caplog):
    """The real catalog query fails on SQLite; startup carries on with a WARNING."""
    with app.app_context(), caplog.at_level(logging.WARNING):
        _as_postgres(monkeypatch)
        _warn_if_request_logs_has_retention(app)
    records = _logged(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "Could not check request_logs for a retention policy" in records[0].getMessage()


def test_failure_leaves_the_session_usable(app, monkeypatch, caplog):
    """A failure that needs a rollback is rolled back before startup continues.

    A failed SELECT does not poison a SQLite session, so this fails a flush
    instead: the session is then pending-rollback, and without the tripwire's
    rollback the next statement raises PendingRollbackError.
    """
    from lumen.models.model_config import ModelConfig

    def failing_flush(conn):
        # Missing required cost fields: the flush raises IntegrityError.
        db.session.add(ModelConfig(model_name="tripwire-flush-failure"))
        db.session.flush()

    monkeypatch.setattr("lumen.commands._retention_job", failing_flush)
    with app.app_context(), caplog.at_level(logging.WARNING):
        _as_postgres(monkeypatch)
        _warn_if_request_logs_has_retention(app)
        assert db.session.execute(db.text("SELECT 1")).scalar() == 1
    assert [r.levelno for r in _logged(caplog)] == [logging.WARNING]
