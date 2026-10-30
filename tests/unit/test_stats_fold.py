"""fold_once: one window of request_logs folded into model_stats / entity_stats.

These run on SQLite, where ``request_logs.time`` comes back naive. The
concurrent-fold case needs real row locks and lives in
``tests/integration/test_stats_fold_postgres.py``.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services.stats_fold import fold_lag, fold_once, get_fold_state
from tests.conftest import fold

LO = datetime(2026, 10, 1, 12, 0, 0)
HI = LO + timedelta(minutes=10)
US = timedelta(microseconds=1)


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


@pytest.fixture
def ids(ctx):
    """Two entities and two models."""
    entities = [Entity(entity_type="user", email=f"fold{i}@example.com", name=f"Fold {i}",
                       initials="FO", active=True) for i in range(2)]
    models = [ModelConfig(model_name=f"fold-model-{i}", input_cost_per_million=1.0,
                          output_cost_per_million=1.0) for i in range(2)]
    db.session.add_all(entities + models)
    db.session.commit()
    return [e.id for e in entities], [m.id for m in models]


def _state(folded_until=LO, cutover_at=LO):
    db.session.add(StatsFoldState(id=1, cutover_at=cutover_at, folded_until=folded_until))
    db.session.commit()


def _log(t, entity_id, model_id, source="api", input_tokens=10, output_tokens=5, audio_seconds=0, cost="0.5"):
    """Write a request_logs row stamped at naive-UTC ``t``, the way billing does (aware)."""
    db.session.add(RequestLog(
        time=t.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source=source,
        input_tokens=input_tokens, output_tokens=output_tokens, audio_seconds=audio_seconds,
        cost=Decimal(cost), duration=0.0,
    ))
    db.session.commit()


def _fold_to(app, hi):
    """Fold with ``now`` chosen so the window ends exactly at ``hi``."""
    return fold_once(app, now=hi + fold_lag(app))


def _model_stats():
    db.session.expire_all()
    return {(s.entity_id, s.model_config_id, s.source): s
            for s in db.session.execute(select(ModelStat)).scalars()}


def _entity_stat(entity_id):
    db.session.expire_all()
    return db.session.get(EntityStat, entity_id)


def test_fold_sums_exactly_the_half_open_window_and_creates_rows(app, ids):
    (e1, _), (m1, m2) = ids
    _state()
    _log(LO - US, e1, m1)                                  # before the window
    _log(LO, e1, m1, input_tokens=1, output_tokens=2, audio_seconds=3, cost="1.25")
    _log(LO + timedelta(minutes=5), e1, m1, input_tokens=10, output_tokens=20, audio_seconds=30, cost="2.5")
    _log(LO + timedelta(minutes=6), e1, m2, source="chat", input_tokens=100, cost="0.25")
    _log(HI, e1, m1)                                       # at hi: next window

    assert _fold_to(app, HI) == HI

    stats = _model_stats()
    assert set(stats) == {(e1, m1, "api"), (e1, m2, "chat")}
    s = stats[(e1, m1, "api")]
    assert (s.requests, s.input_tokens, s.output_tokens, s.audio_seconds, s.cost) == (2, 11, 22, 33, Decimal("3.75"))
    assert s.last_used_at == LO + timedelta(minutes=5)
    es = _entity_stat(e1)
    assert (es.requests, es.input_tokens, es.output_tokens, es.audio_seconds, es.cost) == (3, 111, 27, 33, Decimal("4.0"))
    assert es.last_used_at == LO + timedelta(minutes=6)
    assert get_fold_state().folded_until == HI


def test_row_at_hi_is_counted_once_by_the_next_pass(app, ids):
    (e1, _), (m1, _) = ids
    _state()
    _log(HI, e1, m1)
    _fold_to(app, HI)
    assert _entity_stat(e1) is None

    later = HI + timedelta(minutes=1)
    assert _fold_to(app, later) == later
    assert _entity_stat(e1).requests == 1
    assert _model_stats()[(e1, m1, "api")].requests == 1

    # A further pass with no new rows adds nothing; an empty window is a no-op.
    _fold_to(app, later + timedelta(minutes=1))
    assert _fold_to(app, later) is None
    assert _entity_stat(e1).requests == 1
    assert _model_stats()[(e1, m1, "api")].requests == 1


def test_entity_last_used_at_is_the_latest_across_models(app, ids):
    """Model A at t2 and model B at t1 < t2: the entity ends at t2 whatever the group order."""
    (e1, _), (m1, m2) = ids
    t1, t2 = LO + timedelta(minutes=1), LO + timedelta(minutes=2)
    _state()
    _log(t2, e1, m1)
    _log(t1, e1, m2)
    _fold_to(app, HI)
    assert _entity_stat(e1).last_used_at == t2
    assert _model_stats()[(e1, m2, "api")].last_used_at == t1


def test_later_existing_last_used_at_is_not_overwritten(app, ids):
    (e1, _), (m1, _) = ids
    later = HI + timedelta(hours=1)
    db.session.add(EntityStat(entity_id=e1, requests=5, input_tokens=0, output_tokens=0, audio_seconds=0,
                              cost=0, conversations=0, last_used_at=later))
    db.session.add(ModelStat(entity_id=e1, model_config_id=m1, source="api", requests=5, input_tokens=0,
                             output_tokens=0, audio_seconds=0, cost=0, last_used_at=later))
    db.session.commit()
    _state()
    _log(LO + timedelta(minutes=1), e1, m1)
    _fold_to(app, HI)

    es = _entity_stat(e1)
    assert (es.requests, es.last_used_at) == (6, later)
    ms = _model_stats()[(e1, m1, "api")]
    assert (ms.requests, ms.last_used_at) == (6, later)


def test_earlier_existing_last_used_at_moves_forward(app, ids):
    (e1, _), (m1, _) = ids
    db.session.add(EntityStat(entity_id=e1, requests=1, input_tokens=0, output_tokens=0, audio_seconds=0,
                              cost=0, conversations=0, last_used_at=LO - timedelta(days=1)))
    db.session.commit()
    _state()
    _log(LO + timedelta(minutes=3), e1, m1)
    _fold_to(app, HI)
    assert _entity_stat(e1).last_used_at == LO + timedelta(minutes=3)


def test_null_keys_are_skipped_and_the_watermark_still_advances(app, ids):
    (e1, _), (m1, _) = ids
    _state()
    _log(LO + timedelta(minutes=1), None, m1)
    _log(LO + timedelta(minutes=2), e1, None, input_tokens=7)

    assert _fold_to(app, HI) == HI
    assert _model_stats() == {}
    es = _entity_stat(e1)
    assert (es.requests, es.input_tokens) == (1, 7)
    assert db.session.execute(select(EntityStat)).scalars().all() == [es]
    assert get_fold_state().folded_until == HI


def test_no_state_row_is_a_no_op(app, ids):
    (e1, _), (m1, _) = ids
    _log(LO + timedelta(minutes=1), e1, m1)
    assert get_fold_state() == (None, None)
    assert fold(app) is None
    assert _entity_stat(e1) is None
    assert db.session.execute(select(StatsFoldState)).scalars().all() == []


def test_null_cutover_is_a_no_op(app, ids):
    (e1, _), (m1, _) = ids
    _state(folded_until=None, cutover_at=None)
    _log(LO + timedelta(minutes=1), e1, m1)
    assert fold(app) is None
    assert _entity_stat(e1) is None
    assert get_fold_state() == (None, None)


def test_null_cutover_with_a_watermark_is_a_no_op(app, ids):
    """The gate is the cutover itself, not only the watermark next to it."""
    (e1, _), (m1, _) = ids
    _state(cutover_at=None)
    _log(LO + timedelta(minutes=1), e1, m1)
    assert fold(app) is None
    assert _entity_stat(e1) is None
    assert get_fold_state() == (None, LO)


def test_window_not_yet_past_the_lag_is_a_no_op(app, ids):
    (e1, _), (m1, _) = ids
    _state()
    _log(LO, e1, m1)
    assert fold_once(app, now=LO + fold_lag(app) - US) is None
    assert _entity_stat(e1) is None
    assert get_fold_state().folded_until == LO


def test_aware_now_is_accepted(app, ids):
    (e1, _), (m1, _) = ids
    _state()
    _log(LO, e1, m1)
    assert fold_once(app, now=(HI + fold_lag(app)).replace(tzinfo=timezone.utc)) == HI
    assert _entity_stat(e1).requests == 1


def test_lost_compare_and_set_rolls_the_pass_back(app, ids, monkeypatch):
    """Another worker moved the watermark between this pass's read and its UPDATE."""
    from lumen.services import stats_fold
    (e1, _), (m1, _) = ids
    _state()
    _log(LO, e1, m1)
    real = stats_fold.get_fold_state

    def stale_read():
        state = real()
        db.session.execute(StatsFoldState.__table__.update().values(folded_until=LO + US))
        return state

    monkeypatch.setattr(stats_fold, "get_fold_state", stale_read)
    assert _fold_to(app, HI) is None
    monkeypatch.undo()
    assert _entity_stat(e1) is None
    assert _model_stats() == {}


def test_post_cutover_fixture_and_fold_helper(app, ids, post_cutover):
    (e1, _), (m1, _) = ids
    state = get_fold_state()
    assert state.cutover_at is not None and state.cutover_at == state.folded_until
    _log(state.cutover_at + timedelta(minutes=1), e1, m1)
    assert fold(app) is not None
    assert _entity_stat(e1).requests == 1


def test_bump_conflict_branch_keeps_the_later_last_used_at(ids, monkeypatch):
    """The ON CONFLICT branch applies the same never-backwards rule as the UPDATE."""
    from sqlalchemy.sql.dml import Update

    from lumen.services.llm import _bump
    (e1, _), _ = ids
    later = HI + timedelta(hours=1)
    db.session.add(EntityStat(entity_id=e1, requests=1, input_tokens=0, output_tokens=0, audio_seconds=0,
                              cost=0, conversations=0, last_used_at=later))
    db.session.commit()

    real_execute = db.session.execute

    class _NoRows:
        rowcount = 0

    def execute(statement, *args, **kwargs):
        if isinstance(statement, Update):
            return _NoRows()
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db.session, "execute", execute)
    _bump(EntityStat, {"entity_id": e1}, {"requests": EntityStat.requests + 1}, {"requests": 1},
          last_used_at=LO)
    monkeypatch.undo()
    db.session.commit()
    es = _entity_stat(e1)
    assert (es.requests, es.last_used_at) == (2, later)


def test_fold_leaves_the_callers_session_alone(app, ids):
    """fold_once pushes its own app context: the caller's pending work is neither committed nor dropped."""
    (e1, _), (m1, _) = ids
    _state()
    _log(LO, e1, m1)
    pending = Entity(entity_type="user", email="pending@example.com", name="Pending", initials="PE", active=True)
    db.session.add(pending)

    assert _fold_to(app, HI) == HI
    assert pending in db.session.new
    db.session.rollback()
    assert db.session.execute(select(Entity).filter_by(email="pending@example.com")).scalar_one_or_none() is None
    assert _entity_stat(e1).requests == 1


def test_bump_rejects_last_used_at_given_twice(ids):
    from lumen.services.llm import _bump
    (e1, _), _ = ids
    with pytest.raises(AssertionError):
        _bump(EntityStat, {"entity_id": e1}, {"last_used_at": LO}, {}, last_used_at=LO)
