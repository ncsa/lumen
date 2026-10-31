"""Billing on both sides of the stats cutover, and the cached ``cutover_at``.

Before the cutover billing bumps ``model_stats`` / ``entity_stats`` per
request; from ``cutover_at`` on it leaves them to the fold. Every request must
be counted exactly once, on one side or the other.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services import llm, stats_cutover
from lumen.services.llm import bill_request, update_stats
from lumen.services.stats_cutover import CUTOVER_CACHE_TTL, cached_cutover_at
from lumen.services.stats_fold import fold_lag, fold_once
from lumen.timeutils import utcnow
from tests.conftest import fold

C = datetime(2026, 10, 1, 12, 0, 0)
US = timedelta(microseconds=1)


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


@pytest.fixture
def ids(ctx):
    entity = Entity(entity_type="user", email="cutover@example.com", name="Cut", initials="CU", active=True)
    model = ModelConfig(model_name="cutover-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
    db.session.add_all([entity, model])
    db.session.commit()
    return entity.id, model.id


def _cut_over_at(at):
    db.session.add(StatsFoldState(id=1, cutover_at=at, folded_until=at))
    db.session.commit()
    stats_cutover.clear_cutover_cache()


def _bill_at(monkeypatch, t, entity_id, model_id, input_tokens=10, output_tokens=5, cost=0.5):
    """Bill one request whose request_logs row is stamped at naive-UTC ``t``."""
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return t.replace(tzinfo=timezone.utc)

    with monkeypatch.context() as m:
        m.setattr(llm, "datetime", _Clock)
        update_stats(entity_id, model_id, "api", input_tokens, output_tokens, cost)
    db.session.commit()


def _markers(entity_id):
    """``stats_deferred`` of the entity's request_logs rows, oldest first."""
    db.session.expire_all()
    return db.session.execute(
        select(RequestLog.stats_deferred).where(RequestLog.entity_id == entity_id)
        .order_by(RequestLog.time, RequestLog.id)
    ).scalars().all()


def _old_pod_bill(entity_id, model_id, source="api", input_tokens=10, output_tokens=5, cost=0.5):
    """Bill the way the previous release does: an INSERT that knows nothing of
    ``stats_deferred`` (so NULL), then both bumps, whatever the cutover says."""
    now = utcnow()
    db.session.add(RequestLog(
        time=now.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source=source,
        input_tokens=input_tokens, output_tokens=output_tokens, cost=cost, duration=0.0,
    ))
    db.session.flush()
    for model, key in ((ModelStat, {"entity_id": entity_id, "model_config_id": model_id, "source": source}),
                       (EntityStat, {"entity_id": entity_id})):
        llm._bump(
            model, key,
            {"requests": model.requests + 1, "input_tokens": model.input_tokens + input_tokens,
             "output_tokens": model.output_tokens + output_tokens, "cost": model.cost + cost,
             "last_used_at": now},
            {"requests": 1, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost,
             "last_used_at": now},
        )
    db.session.commit()


def _requests(entity_id, model_id):
    db.session.expire_all()
    entity = db.session.get(EntityStat, entity_id)
    model_requests = db.session.execute(
        select(func.coalesce(func.sum(ModelStat.requests), 0))
        .where(ModelStat.entity_id == entity_id, ModelStat.model_config_id == model_id)
    ).scalar_one()
    return (entity.requests if entity else 0), model_requests


# ---------------------------------------------------------------------------
# Exactly once, on one side or the other
# ---------------------------------------------------------------------------

def test_rows_around_the_cutover_are_each_counted_once(app, ids, monkeypatch):
    entity_id, model_id = ids
    _cut_over_at(C)
    _bill_at(monkeypatch, C - US, entity_id, model_id)  # bumped by billing
    _bill_at(monkeypatch, C, entity_id, model_id)       # left to the fold
    _bill_at(monkeypatch, C + US, entity_id, model_id)  # left to the fold
    assert _requests(entity_id, model_id) == (1, 1)
    assert _markers(entity_id) == [None, True, True]

    assert fold_once(app, now=C + timedelta(minutes=1) + fold_lag(app)) is not None
    assert _requests(entity_id, model_id) == (3, 3)


def test_parity_with_history_on_both_sides_of_the_cutover(app, ids, monkeypatch):
    """Totals from before request_logs existed, plus N billed before and M after, plus a fold."""
    entity_id, model_id = ids
    db.session.add(EntityStat(entity_id=entity_id, requests=100, input_tokens=1000, output_tokens=500,
                              audio_seconds=7, cost=Decimal("12.5"), conversations=3))
    db.session.add(ModelStat(entity_id=entity_id, model_config_id=model_id, source="api", requests=100,
                             input_tokens=1000, output_tokens=500, audio_seconds=7, cost=Decimal("12.5")))
    db.session.commit()
    _cut_over_at(C)
    for i in range(3):
        _bill_at(monkeypatch, C - timedelta(seconds=10 - i), entity_id, model_id, 10, 5, 0.25)
    for i in range(2):
        _bill_at(monkeypatch, C + timedelta(seconds=1 + i), entity_id, model_id, 20, 7, 0.5)
    fold_once(app, now=C + timedelta(minutes=1) + fold_lag(app))

    db.session.expire_all()
    logs = db.session.execute(select(
        func.count(), func.sum(RequestLog.input_tokens), func.sum(RequestLog.output_tokens),
        func.sum(RequestLog.cost),
    ).where(RequestLog.entity_id == entity_id)).one()
    for stat in (db.session.get(EntityStat, entity_id),
                 db.session.execute(select(ModelStat).where(ModelStat.entity_id == entity_id)).scalar_one()):
        assert stat.requests == 100 + logs[0] == 105
        assert stat.input_tokens == 1000 + logs[1]
        assert stat.output_tokens == 500 + logs[2]
        assert Decimal(str(stat.cost)) == Decimal("12.5") + Decimal(str(logs[3]))
    assert db.session.get(EntityStat, entity_id).conversations == 3


def test_before_cutover_billing_still_bumps(app, ids):
    """No stats_fold_state row (a create_all database): the old path, unchanged."""
    entity_id, model_id = ids
    update_stats(entity_id, model_id, "api", 10, 5, 0.5)
    db.session.commit()
    assert _requests(entity_id, model_id) == (1, 1)
    assert fold(app) is None


def test_null_cutover_still_bumps(app, ids):
    """The row exists with both columns NULL (PostgreSQL between e4f5a6b7c8d9 and
    the stats_deferred_marker migration): billing bumps and leaves the row unmarked."""
    entity_id, model_id = ids
    db.session.add(StatsFoldState(id=1, cutover_at=None, folded_until=None))
    db.session.commit()
    update_stats(entity_id, model_id, "api", 10, 5, 0.5)
    db.session.commit()
    assert _requests(entity_id, model_id) == (1, 1)
    assert _markers(entity_id) == [None]


def test_post_cutover_billing_then_fold_counts_once(app, ids, post_cutover):
    entity_id, model_id = ids
    bill_request(entity_id, model_id, "api", 10, 5, 0.5, effective=None)
    assert _requests(entity_id, model_id) == (0, 0)
    fold(app)
    assert _requests(entity_id, model_id) == (1, 1)


_REVISION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "e4f5a6b7c8d9_stats_fold_state.py"
)


def _run_revision_upgrade(engine):
    spec = importlib.util.spec_from_file_location("stats_fold_state_revision", _REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.connect() as conn:
        mctx = MigrationContext.configure(conn)
        with mctx.begin_transaction():
            with Operations.context(mctx):
                module.upgrade()
        conn.commit()


def test_migrated_sqlite_database_counts_each_billed_request_once(app, ids):
    """The SQLite deploy this PR ships: the revision cuts over at migration time,
    then billing and the fold thread both run. A request must not land twice."""
    entity_id, model_id = ids
    # Rebuild stats_fold_state exactly as the migration leaves it on SQLite.
    StatsFoldState.__table__.drop(db.engine)
    db.session.remove()
    _run_revision_upgrade(db.engine)
    assert db.session.get(StatsFoldState, 1).cutover_at is not None

    bill_request(entity_id, model_id, "api", 10, 5, 0.5, effective=None)
    bill_request(entity_id, model_id, "chat", 3, 2, 0.1, effective=None)
    assert _requests(entity_id, model_id) == (0, 0)

    assert fold(app) is not None
    fold(app)  # a later pass: nothing new to add
    assert _requests(entity_id, model_id) == (2, 2)
    db.session.expire_all()
    assert db.session.get(EntityStat, entity_id).input_tokens == 13


def test_rolling_update_counts_every_request_once(app, ids):
    """Old and new pods bill side by side after the migration's automatic cutover.

    The old pods bump and write unmarked rows; the new pods mark theirs and leave
    them to the fold. However the two interleave, the totals after a fold are the
    previous totals plus every request exactly once.
    """
    entity_id, model_id = ids
    db.session.add(EntityStat(entity_id=entity_id, requests=50, input_tokens=500, output_tokens=250,
                              audio_seconds=0, cost=Decimal("5"), conversations=0))
    db.session.add(ModelStat(entity_id=entity_id, model_config_id=model_id, source="api", requests=50,
                             input_tokens=500, output_tokens=250, audio_seconds=0, cost=Decimal("5")))
    db.session.commit()
    _cut_over_at(utcnow() - timedelta(hours=1))  # what the migration sets

    for i in range(4):
        _old_pod_bill(entity_id, model_id, input_tokens=10, output_tokens=5, cost=0.5)
        bill_request(entity_id, model_id, "api", 20, 7, 0.25, effective=None)
        if i % 2:
            _old_pod_bill(entity_id, model_id, input_tokens=1, output_tokens=1, cost=0.125)
    assert _markers(entity_id).count(True) == 4
    assert _markers(entity_id).count(None) == 6

    assert fold(app) is not None
    fold(app)  # a later pass adds nothing

    db.session.expire_all()
    for stat in (db.session.get(EntityStat, entity_id),
                 db.session.execute(select(ModelStat).where(ModelStat.entity_id == entity_id)).scalar_one()):
        assert stat.requests == 50 + 10
        assert stat.input_tokens == 500 + 4 * 10 + 4 * 20 + 2 * 1
        assert stat.output_tokens == 250 + 4 * 5 + 4 * 7 + 2 * 1
        assert Decimal(str(stat.cost)) == Decimal("5") + 4 * Decimal("0.5") + 4 * Decimal("0.25") + 2 * Decimal("0.125")


def test_stale_cached_cutover_bumps_unmarked_and_the_fold_skips_it(app, ids):
    """A process that cached ``None`` just before the cutover was set keeps bumping
    for up to the cache TTL. Its rows are unmarked, so the fold leaves them alone."""
    entity_id, model_id = ids
    assert cached_cutover_at() is None  # cached before the cutover exists
    _cut_over_at_without_clearing(utcnow() - timedelta(hours=1))

    update_stats(entity_id, model_id, "api", 10, 5, 0.5)
    db.session.commit()
    assert _requests(entity_id, model_id) == (1, 1)
    assert _markers(entity_id) == [None]

    # Once the cache expires, the same process defers its next request.
    stats_cutover.clear_cutover_cache()
    update_stats(entity_id, model_id, "api", 10, 5, 0.5)
    db.session.commit()
    assert _requests(entity_id, model_id) == (1, 1)
    assert _markers(entity_id) == [None, True]

    # The fold adds only the deferred one: two requests, each counted once.
    assert fold(app) is not None
    assert _requests(entity_id, model_id) == (2, 2)


# ---------------------------------------------------------------------------
# The cached cutover_at
# ---------------------------------------------------------------------------

def test_cutover_at_is_cached_for_the_ttl(ctx, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(stats_cutover.time, "monotonic", lambda: clock[0])
    assert cached_cutover_at() is None  # no row yet

    _cut_over_at_without_clearing(C)
    clock[0] += CUTOVER_CACHE_TTL - 1
    assert cached_cutover_at() is None  # still the cached read

    clock[0] += 1
    assert cached_cutover_at() == C


def _cut_over_at_without_clearing(at):
    db.session.add(StatsFoldState(id=1, cutover_at=at, folded_until=at))
    db.session.commit()


def test_cached_read_is_one_select_per_ttl(ctx, monkeypatch):
    from sqlalchemy import event

    _cut_over_at(C)
    reads = []

    def on_execute(conn, cursor, statement, *args):
        if "stats_fold_state" in statement:
            reads.append(statement)

    event.listen(db.engine, "before_cursor_execute", on_execute)
    try:
        for _ in range(5):
            assert cached_cutover_at() == C
    finally:
        event.remove(db.engine, "before_cursor_execute", on_execute)
    assert len(reads) == 1


def test_cache_is_primed_here(ctx):
    _cut_over_at(C)
    assert cached_cutover_at() == C
    assert stats_cutover._cached is not None


def test_cache_starts_empty_in_every_test(ctx):
    """The autouse ``clear_cutover_cache`` fixture: nothing survives from the test above."""
    assert stats_cutover._cached is None
    assert cached_cutover_at() is None
