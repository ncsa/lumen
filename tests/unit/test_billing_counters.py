"""Billing counters: UPDATE first, INSERT … ON CONFLICT only on an entity's first use.

The steady-state test records every statement the way
``tests/routes/test_preflight_queries.py`` does: once the counter rows exist,
``update_stats`` + ``subtract_coins`` run only their UPDATEs and the RequestLog
INSERT — no existence SELECT and no SAVEPOINT.

The fallback tests force the UPDATE to match nothing while the row does exist,
which is what a concurrent first request looks like from the loser's side: the
row appears between its UPDATE and its INSERT. The ON CONFLICT branch then has
to apply the increment to the existing row, exactly once.
"""
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.sql.dml import Update

from lumen.services.stats_cutover import cached_cutover_at

STARTING = 100
EFFECTIVE = (100.0, 0.0, float(STARTING))


@contextmanager
def _statements():
    from lumen.extensions import db
    log = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        log.append(" ".join(statement.split()))

    event.listen(db.engine, "before_cursor_execute", _record)
    try:
        yield log
    finally:
        event.remove(db.engine, "before_cursor_execute", _record)


@contextmanager
def _updates_match_nothing(monkeypatch):
    """Make every UPDATE report ``rowcount == 0`` without touching the database."""
    from lumen.extensions import db
    real_execute = db.session.execute

    class _NoRows:
        rowcount = 0

    def execute(statement, *args, **kwargs):
        if isinstance(statement, Update):
            return _NoRows()
        return real_execute(statement, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(db.session, "execute", execute)
        yield


def _seed(entity_id):
    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=STARTING))
    db.session.commit()


def _bill(entity_id, model_id, cost=2.0):
    from lumen.extensions import db
    from lumen.services.llm import subtract_coins, update_stats
    update_stats(entity_id, model_id, "api", 10, 5, cost)
    subtract_coins(entity_id, model_id, cost, effective=EFFECTIVE)
    db.session.commit()


def _counters(entity_id, model_id):
    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_stat import ModelStat
    db.session.expire_all()
    ms = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id, source="api")).scalar_one()
    es = db.session.get(EntityStat, entity_id)
    bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
    return ms, es, bal


def test_steady_state_billing_runs_no_select_and_no_savepoint(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _seed(entity_id)
        _bill(entity_id, model_id)  # first use creates the rows

        with _statements() as log:
            _bill(entity_id, model_id)

        verbs = [s.split()[0].upper() for s in log]
        assert "SELECT" not in verbs, log
        assert not [s for s in log if "SAVEPOINT" in s.upper()], log
        assert verbs == ["INSERT", "UPDATE", "UPDATE", "UPDATE"], log
        assert "INTO request_logs" in log[0]


def test_first_use_inserts_each_counter_row_once(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _seed(entity_id)
        # Billing reads stats_fold_state.cutover_at once per CUTOVER_CACHE_TTL
        # (test_billing_cutover pins that); this test is about the counters.
        cached_cutover_at()
        with _statements() as log:
            _bill(entity_id, model_id, cost=3.0)

        assert not [s for s in log if s.upper().startswith("SELECT")], log
        upserts = [s for s in log if "ON CONFLICT" in s]
        assert [s.split()[2] for s in upserts] == ["model_stats", "entity_stats", "entity_balances"]

        ms, es, bal = _counters(entity_id, model_id)
        assert (ms.requests, ms.input_tokens, ms.output_tokens) == (1, 10, 5)
        assert (es.requests, es.input_tokens, es.output_tokens) == (1, 10, 5)
        assert float(bal.coins_left) == STARTING - 3.0
        assert bal.last_refill_at is not None


def test_first_use_balance_is_floored_at_zero(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _seed(entity_id)
        _bill(entity_id, model_id, cost=STARTING + 50)
        _, _, bal = _counters(entity_id, model_id)
        assert float(bal.coins_left) == 0


def test_on_conflict_fallback_adds_exactly_once(app, test_user, test_model, monkeypatch):
    """Rows exist but the UPDATE matched none: ON CONFLICT bumps the existing rows once."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _seed(entity_id)
        _bill(entity_id, model_id, cost=2.0)

        with _updates_match_nothing(monkeypatch), _statements() as log:
            _bill(entity_id, model_id, cost=5.0)

        assert len([s for s in log if "ON CONFLICT" in s]) == 3, log
        ms, es, bal = _counters(entity_id, model_id)
        assert (ms.requests, ms.input_tokens, ms.output_tokens) == (2, 20, 10)
        assert float(ms.cost) == pytest.approx(7.0)
        assert (es.requests, es.input_tokens, es.output_tokens) == (2, 20, 10)
        assert float(es.cost) == pytest.approx(7.0)
        assert float(bal.coins_left) == STARTING - 2.0 - 5.0


def test_on_conflict_fallback_floors_the_existing_balance(app, test_user, test_model, monkeypatch):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        _seed(entity_id)
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=Decimal("4")))
        db.session.commit()

        with _updates_match_nothing(monkeypatch):
            _bill(entity_id, model_id, cost=10.0)

        _, _, bal = _counters(entity_id, model_id)
        assert float(bal.coins_left) == 0


def test_conversation_counter_first_use_and_fallback(app, test_user, monkeypatch):
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.blueprints.chat.routes import _count_conversation_started
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat

        with _statements() as log:
            _count_conversation_started(entity_id)
            db.session.commit()
        assert not [s for s in log if s.upper().startswith("SELECT")], log
        db.session.expire_all()
        es = db.session.get(EntityStat, entity_id)
        assert (es.conversations, es.requests) == (1, 0)

        with _updates_match_nothing(monkeypatch):
            _count_conversation_started(entity_id)
            db.session.commit()
        db.session.expire_all()
        assert db.session.get(EntityStat, entity_id).conversations == 2
