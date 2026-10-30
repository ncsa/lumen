"""The stats fold thread and the per-pass guards around fold_once.

Covers what step 1 left to the wiring: the window cap, a locked SQLite database
skipped as "retry next pass", and the thread reading ``stats.fold_interval`` /
``stats.fold_lag`` from ``app.config`` on every pass.
"""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.models.request_log import RequestLog
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services import stats_fold
from lumen.services.stats_fold import MAX_FOLD_WINDOW, fold_lag, fold_once, get_fold_state, run_fold_pass
from lumen.timeutils import utcnow

LO = datetime(2026, 10, 1, 12, 0, 0)


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


@pytest.fixture
def ids(ctx):
    entity = Entity(entity_type="user", email="foldthread@example.com", name="Fold", initials="FO", active=True)
    model = ModelConfig(model_name="fold-thread-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
    db.session.add_all([entity, model])
    db.session.commit()
    return entity.id, model.id


def _state(at):
    db.session.add(StatsFoldState(id=1, cutover_at=at, folded_until=at))
    db.session.commit()


def _log(t, entity_id, model_id):
    db.session.add(RequestLog(
        time=t.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source="api",
        input_tokens=10, output_tokens=5, audio_seconds=0, cost=Decimal("0.5"), duration=0.0,
    ))
    db.session.commit()


def _requests(entity_id):
    db.session.expire_all()
    stat = db.session.get(EntityStat, entity_id)
    return stat.requests if stat else 0


def _watermark():
    db.session.expire_all()
    return get_fold_state().folded_until


# ---------------------------------------------------------------------------
# Window cap
# ---------------------------------------------------------------------------

def test_a_long_stall_is_folded_one_capped_window_at_a_time(app, ids):
    entity_id, model_id = ids
    _state(LO)
    _log(LO + timedelta(hours=1), entity_id, model_id)
    _log(LO + MAX_FOLD_WINDOW + timedelta(hours=1), entity_id, model_id)
    now = LO + 3 * MAX_FOLD_WINDOW + fold_lag(app)

    assert fold_once(app, now=now) == LO + MAX_FOLD_WINDOW
    assert _requests(entity_id) == 1
    assert fold_once(app, now=now) == LO + 2 * MAX_FOLD_WINDOW
    assert _requests(entity_id) == 2
    assert fold_once(app, now=now) == LO + 3 * MAX_FOLD_WINDOW
    assert fold_once(app, now=now) is None
    assert _requests(entity_id) == 2


# ---------------------------------------------------------------------------
# Locked SQLite database
# ---------------------------------------------------------------------------

def test_locked_sqlite_database_is_left_for_the_next_pass(app, ids, monkeypatch):
    entity_id, model_id = ids
    at = utcnow() - timedelta(hours=1)
    _state(at)
    _log(at + timedelta(minutes=1), entity_id, model_id)
    monkeypatch.setattr(stats_fold, "_SQLITE_BUSY_TIMEOUT_MS", 50)

    # Another connection holds the write lock, as a request mid-write does.
    holder = sqlite3.connect(db.engine.url.database, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        assert run_fold_pass(app) is None
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _watermark() == at
    assert _requests(entity_id) == 0

    assert run_fold_pass(app) is not None
    assert _requests(entity_id) == 1


def test_other_operational_errors_propagate(app, monkeypatch):
    def boom(app, now=None):
        raise OperationalError("SELECT 1", {}, Exception("no such table: stats_fold_state"))

    monkeypatch.setattr(stats_fold, "fold_once", boom)
    with pytest.raises(OperationalError):
        run_fold_pass(app)


# ---------------------------------------------------------------------------
# The thread
# ---------------------------------------------------------------------------

def _drive_thread(app, monkeypatch, on_sleep):
    """Run the real fold thread; ``on_sleep(call_no, seconds, stop)`` replaces each sleep."""
    passes, sleeps = [], []
    real_pass = stats_fold.run_fold_pass

    def recording_pass(app):
        hi = real_pass(app)
        passes.append(hi)
        return hi

    def fake_sleep(seconds, stop):
        sleeps.append(seconds)
        on_sleep(len(sleeps), seconds, stop)

    monkeypatch.setattr(stats_fold, "run_fold_pass", recording_pass)
    monkeypatch.setattr(stats_fold, "_sleep", fake_sleep)
    thread = stats_fold.start_fold_thread(app)
    thread.join(30)
    assert not thread.is_alive()
    stats_fold.stop_fold_thread()
    return passes, sleeps


def test_thread_reads_interval_and_lag_from_config_on_every_pass(app, ids, monkeypatch, restore_config):
    at = utcnow() - timedelta(hours=1)
    _state(at)
    app.config["STATS_FOLD_INTERVAL"] = 60
    app.config["STATS_FOLD_LAG"] = 300

    def on_sleep(n, seconds, stop):
        if n == 1:
            # A config reload between passes.
            app.config["STATS_FOLD_INTERVAL"] = 7
            app.config["STATS_FOLD_LAG"] = 11
        else:
            stop.set()

    before = utcnow()
    passes, sleeps = _drive_thread(app, monkeypatch, on_sleep)
    after = utcnow()

    assert len(passes) == 2
    assert before - timedelta(seconds=300) <= passes[0] <= after - timedelta(seconds=300)
    assert before - timedelta(seconds=11) <= passes[1] <= after - timedelta(seconds=11)
    assert 54 <= sleeps[0] <= 66
    assert 6.3 <= sleeps[1] <= 7.7


def test_thread_catches_up_without_sleeping_between_capped_passes(app, ids, monkeypatch):
    at = utcnow() - 3 * MAX_FOLD_WINDOW - fold_lag(app) - timedelta(hours=1)
    _state(at)

    passes, sleeps = _drive_thread(app, monkeypatch, lambda n, seconds, stop: stop.set())

    assert passes[:3] == [at + MAX_FOLD_WINDOW, at + 2 * MAX_FOLD_WINDOW, at + 3 * MAX_FOLD_WINDOW]
    assert len(passes) == 4 and passes[3] is not None
    assert len(sleeps) == 1


def test_thread_before_cutover_is_a_no_op(app, ids, monkeypatch):
    entity_id, model_id = ids
    _log(utcnow() - timedelta(hours=1), entity_id, model_id)

    passes, sleeps = _drive_thread(app, monkeypatch, lambda n, seconds, stop: stop.set())

    assert passes == [None]
    assert _requests(entity_id) == 0
    assert db.session.execute(select(StatsFoldState)).first() is None


def test_thread_survives_a_failed_pass(app, monkeypatch, caplog):
    calls = []

    def flaky(app):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(stats_fold, "run_fold_pass", flaky)
    stop_after_two = threading.Event()

    def fake_sleep(seconds, stop):
        if len(calls) >= 2:
            stop.set()
            stop_after_two.set()

    monkeypatch.setattr(stats_fold, "_sleep", fake_sleep)
    thread = stats_fold.start_fold_thread(app)
    thread.join(30)
    stats_fold.stop_fold_thread()

    assert stop_after_two.is_set()
    assert len(calls) == 2
    assert any("stats fold error" in r.getMessage() for r in caplog.records)


def test_stop_fold_thread_ends_a_sleeping_thread(app, monkeypatch):
    monkeypatch.setattr(stats_fold, "run_fold_pass", lambda app: None)
    thread = stats_fold.start_fold_thread(app)
    stats_fold.stop_fold_thread()
    assert not thread.is_alive()
