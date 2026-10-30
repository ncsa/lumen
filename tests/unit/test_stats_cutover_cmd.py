"""``flask stats-cutover`` and the startup warning while it has not run.

The command sets ``cutover_at`` far enough ahead that every process has re-read
its cached value before the cutover takes effect: a process still holding a
cached ``None`` keeps bumping, and a bump of a request at or after
``cutover_at`` is counted again by the fold, with nothing to repair it.
"""
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from lumen import _warn_if_stats_not_cut_over
from lumen.commands import stats_cutover_cmd
from lumen.extensions import db
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services.stats_cutover import CUTOVER_CACHE_TTL, CUTOVER_LEAD, get_fold_state
from lumen.timeutils import utcnow

#: How much longer than the cache TTL the lead must be, at the least.
MARGIN = timedelta(minutes=1)


def _rows(app):
    with app.app_context():
        return db.session.execute(
            select(StatsFoldState.id, StatsFoldState.cutover_at, StatsFoldState.folded_until)
        ).all()


def _run(app):
    return app.test_cli_runner().invoke(stats_cutover_cmd, [])


def test_lead_outlasts_the_cache_ttl():
    assert CUTOVER_LEAD >= timedelta(seconds=CUTOVER_CACHE_TTL) + MARGIN


def test_inserts_the_row_on_a_create_all_database(app):
    assert _rows(app) == []
    start = utcnow()
    result = _run(app)
    end = utcnow()
    assert result.exit_code == 0, result.output
    [(row_id, cutover_at, folded_until)] = _rows(app)
    assert row_id == 1
    assert cutover_at == folded_until
    # At least TTL plus a margin ahead of the moment the command ran.
    assert cutover_at - end >= timedelta(seconds=CUTOVER_CACHE_TTL) + MARGIN
    assert start + CUTOVER_LEAD <= cutover_at <= end + CUTOVER_LEAD
    assert "Stats cutover set to" in result.output


def test_updates_an_existing_row(app):
    """The PostgreSQL migration leaves the row in place with both columns NULL."""
    with app.app_context():
        db.session.add(StatsFoldState(id=1, cutover_at=None, folded_until=None))
        db.session.commit()
    start = utcnow()
    result = _run(app)
    assert result.exit_code == 0, result.output
    [(_, cutover_at, folded_until)] = _rows(app)
    assert cutover_at == folded_until
    assert cutover_at - start >= CUTOVER_LEAD


def test_refuses_a_second_run(app):
    assert _run(app).exit_code == 0
    first = _rows(app)
    result = _run(app)
    assert result.exit_code == 1
    assert "already set" in result.output
    assert _rows(app) == first


def test_refuses_when_the_migration_cut_over(app):
    """SQLite is cut over by its migration; the command leaves that alone."""
    at = utcnow() - timedelta(days=1)
    with app.app_context():
        db.session.add(StatsFoldState(id=1, cutover_at=at, folded_until=at + timedelta(hours=1)))
        db.session.commit()
    result = _run(app)
    assert result.exit_code == 1
    assert str(at) in result.output
    assert _rows(app) == [(1, at, at + timedelta(hours=1))]


def test_refuses_when_a_concurrent_run_inserted_first(app, monkeypatch):
    """This run saw no row, but another run's INSERT won: the primary key refuses it.

    SQLite cannot interleave a second writer here, so the other run's row is in
    place up front and this run's existence check is made to miss it.
    """
    at = utcnow() + CUTOVER_LEAD
    with app.app_context():
        db.session.add(StatsFoldState(id=1, cutover_at=at, folded_until=at))
        db.session.commit()
        monkeypatch.setattr(db.session, "get", lambda model, pk: None)
        result = _run(app)
        monkeypatch.undo()
    assert result.exit_code == 1, result.output
    assert "already set" in result.output
    assert _rows(app) == [(1, at, at)]


# ---------------------------------------------------------------------------
# Startup warning
# ---------------------------------------------------------------------------

def _warnings(caplog):
    return [r for r in caplog.records if r.name == "lumen" and r.levelno >= logging.WARNING]


@pytest.fixture
def as_postgres(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(db.engine.dialect, "name", "postgresql")
        yield


def test_warns_on_postgres_before_the_cutover(app, as_postgres, caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_stats_not_cut_over(app)
    [record] = _warnings(caplog)
    assert "flask stats-cutover" in record.getMessage()


def test_quiet_on_postgres_after_the_cutover(app, as_postgres, caplog):
    at = utcnow()
    db.session.add(StatsFoldState(id=1, cutover_at=at, folded_until=at))
    db.session.commit()
    with caplog.at_level(logging.WARNING):
        _warn_if_stats_not_cut_over(app)
    assert _warnings(caplog) == []


def test_quiet_on_sqlite(app, caplog):
    with app.app_context(), caplog.at_level(logging.WARNING):
        _warn_if_stats_not_cut_over(app)
    assert _warnings(caplog) == []


def test_read_failure_is_a_warning_and_leaves_the_session_usable(app, as_postgres, monkeypatch, caplog):
    def failing_read():
        raise RuntimeError("no stats_fold_state table")

    monkeypatch.setattr("lumen.services.stats_cutover.get_fold_state", failing_read)
    with caplog.at_level(logging.WARNING):
        _warn_if_stats_not_cut_over(app)
        assert db.session.execute(db.text("SELECT 1")).scalar() == 1
    [record] = _warnings(caplog)
    assert "Could not read the stats cutover" in record.getMessage()
    assert get_fold_state() == (None, None)
