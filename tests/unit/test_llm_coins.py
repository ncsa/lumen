"""Tests for subtract_coins, deduct_coins, get_model_access (needs_ack path), has_model_consent."""
from sqlalchemy import select


def test_subtract_coins_deducts_balance(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert float(bal.coins_left) == 90.0


def test_subtract_coins_noop_when_blocked(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import subtract_coins
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.flush()
        db.session.get(ModelConfig, model_id).owner_entity_id = owner.id
        db.session.commit()
        # No exception, just no-op
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()


def test_subtract_coins_noop_when_unlimited(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()


def test_subtract_coins_deducts_correct_amount(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        subtract_coins(entity_id, model_id, 5.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert float(bal.coins_left) == 95.0


def test_subtract_coins_zeroes_when_insufficient(app, test_user, test_model):
    """A request costing more than the remaining balance zeroes it (soft limit)."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=3))
        db.session.commit()
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(bal.coins_left) == 0.0


def test_subtract_coins_deducts_when_affordable_not_zeroes(app, test_user, test_model):
    """With ample balance the cost is deducted (charged), not zeroed — the atomic
    GREATEST update only floors at 0 when the cost exceeds the balance."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(bal.coins_left) == 90.0


def test_subtract_coins_uses_passed_effective_without_reresolving(app, test_user, test_model):
    """When the caller passes the preflight-resolved limit, subtract_coins must not
    re-resolve model access / the pool limit (the per-request redundancy we removed)."""
    from unittest.mock import patch
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services import llm as llm_mod
        from lumen.services.llm import PoolLimit, subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        with patch.object(llm_mod, "get_effective_limit", side_effect=AssertionError("must not re-resolve")):
            subtract_coins(entity_id, model_id, 5.0, effective=PoolLimit(100.0, 0.0, 100.0))
            db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert float(bal.coins_left) == 95.0


def test_get_model_access_needs_ack_without_consent(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import get_model_access
        db.session.get(ModelConfig, model_id).needs_ack = True
        db.session.commit()
        # needs_ack without consent → access denied
        assert get_model_access(entity_id, model_id) is False


def test_get_model_access_needs_ack_with_consent(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from datetime import datetime, timezone

        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import get_model_access
        db.session.get(ModelConfig, model_id).needs_ack = True
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, consented_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


def test_has_model_consent_true(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from datetime import datetime, timezone

        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.services.llm import has_model_consent
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, consented_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.session.commit()
        assert has_model_consent(entity_id, model_id) is True


def test_has_model_consent_false(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.services.llm import has_model_consent
        assert has_model_consent(entity_id, model_id) is False


def test_subtract_coins_creates_balance_on_first_use(app, test_user, test_model):
    """subtract_coins creates an EntityBalance row on first use and deducts from starting_coins."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.commit()
        # No EntityBalance row — subtract_coins creates one from starting_coins and deducts
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert bal is not None
        assert float(bal.coins_left) == 90.0


def test_subtract_coins_skips_insert_when_balance_exists(app, test_user, test_model):
    """An existing balance row must not trigger an INSERT.

    An unconditional INSERT here fails the entity_id unique constraint on every
    request after the first, logging a duplicate-key ERROR in Postgres for each.
    """
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from sqlalchemy import event

        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()

        inserts = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if "INSERT INTO entity_balances" in statement:
                inserts.append(statement)

        event.listen(db.engine, "before_cursor_execute", record)
        try:
            subtract_coins(entity_id, model_id, 10.0)
            db.session.commit()
        finally:
            event.remove(db.engine, "before_cursor_execute", record)

        assert inserts == []


# ---------------------------------------------------------------------------
# Rejection taxonomy: an exhausted budget, counted and dated
# ---------------------------------------------------------------------------

def _recorder(monkeypatch):
    """Capture every observe_rejection call the code under test makes."""
    recorded = []
    monkeypatch.setattr(
        "lumen.blueprints.metrics.middleware.observe_rejection",
        lambda reason, source, model="": recorded.append((reason, source, model)),
    )
    return recorded


def _exhaust(app, entity_id, refresh_coins=10, refilled_minutes_ago=30):
    from datetime import timedelta

    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.timeutils import utcnow
    db.session.add(EntityLimit(
        entity_id=entity_id, max_coins=100, refresh_coins=refresh_coins, starting_coins=100,
    ))
    db.session.add(EntityBalance(
        entity_id=entity_id, coins_left=0,
        last_refill_at=utcnow() - timedelta(minutes=refilled_minutes_ago),
    ))
    db.session.commit()


def test_coin_budget_rejection_is_counted(app, test_user, test_model, monkeypatch):
    """An exhausted budget is a rejection with a known model, unlike the limiter's."""
    from http import HTTPStatus

    from lumen.services.llm import check_coin_budget
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _exhaust(app, entity_id)
        recorded = _recorder(monkeypatch)
        ok, code, _msg, _eff = check_coin_budget(
            entity_id, model_id, source="chat", model_name=test_model["model_name"],
        )
        assert ok is False
        assert code == HTTPStatus.TOO_MANY_REQUESTS
        assert recorded == [("coin_budget", "chat", test_model["model_name"])]


def test_coin_budget_counting_never_breaks_the_rejection(app, test_user, test_model, monkeypatch):
    """A broken counter must not turn a clean 429 into a 500."""
    from http import HTTPStatus

    from lumen.services.llm import check_coin_budget
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _exhaust(app, entity_id)

        def boom(*a, **kw):
            raise RuntimeError("prometheus is unhappy")

        monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
        ok, code, msg, _eff = check_coin_budget(
            entity_id, model_id, source="chat", model_name=test_model["model_name"],
        )
        assert ok is False
        assert code == HTTPStatus.TOO_MANY_REQUESTS
        assert msg == "Coin budget exhausted"


def test_check_coin_budget_does_not_count_without_a_source(app, test_user, test_model, monkeypatch):
    """Non-request callers (tests, tooling) must not fabricate rejection samples."""
    from lumen.services.llm import check_coin_budget
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        _exhaust(app, entity_id)
        recorded = _recorder(monkeypatch)
        check_coin_budget(entity_id, model_id)
        assert recorded == []


def test_coin_retry_after_tracks_the_next_refill(app, test_user, test_model):
    """Retry-After has to be derived, not constant — clients obey it.

    The refiller credits a balance an hour after its last refill, so the wait
    shrinks as that hour is used up: a balance refilled 30 minutes ago is due in
    ~30 minutes, one refilled 10 minutes ago in ~50.
    """
    from lumen.services.llm import coin_retry_after
    entity_id = test_user["id"]
    with app.app_context():
        _exhaust(app, entity_id, refilled_minutes_ago=30)
        half_way = coin_retry_after(entity_id)
    assert 29 * 60 <= half_way <= 30 * 60

    from datetime import timedelta

    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.timeutils import utcnow
    with app.app_context():
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        bal.last_refill_at = utcnow() - timedelta(minutes=10)
        db.session.commit()
        early = coin_retry_after(entity_id)
    assert 49 * 60 <= early <= 50 * 60
    assert early > half_way


def test_coin_retry_after_is_floored_at_one_second(app, test_user, test_model):
    """A refill already due arrives on the refiller's next pass, not instantly."""
    from lumen.services.llm import coin_retry_after
    entity_id = test_user["id"]
    with app.app_context():
        _exhaust(app, entity_id, refilled_minutes_ago=180)
        assert coin_retry_after(entity_id) == 1


def test_coin_retry_after_is_none_when_nothing_refills(app, test_user, test_model):
    """A pool with refresh_coins=0 never refills; inventing a time would lie."""
    from lumen.services.llm import coin_retry_after
    entity_id = test_user["id"]
    with app.app_context():
        _exhaust(app, entity_id, refresh_coins=0)
        assert coin_retry_after(entity_id) is None


# ---------------------------------------------------------------------------
# Rejection taxonomy: nothing healthy to send to
# ---------------------------------------------------------------------------

def test_no_healthy_endpoint_is_counted_on_the_chat_path(app, test_user, test_model, monkeypatch):
    """The chat stream picks its endpoint inside the generator, not in the view.

    So the only place that knows the request was refused for want of a backend
    is the selection itself; counting anywhere else would miss it entirely.
    """
    import pytest

    from lumen.services.llm import send_message_stream
    entity_id = test_user["id"]
    with app.app_context():
        recorded = _recorder(monkeypatch)
        # No ModelEndpoint rows exist for test_model — nothing to select.
        stream = send_message_stream(
            [{"role": "user", "content": "hi"}], test_model["model_name"],
            entity_id=entity_id, source="chat",
        )
        with pytest.raises(RuntimeError, match="No healthy endpoints"):
            next(stream)
        assert recorded == [("no_healthy_endpoint", "chat", test_model["model_name"])]


def test_no_healthy_endpoint_counting_never_breaks_the_error(app, test_user, test_model, monkeypatch):
    """A broken counter must leave the RuntimeError exactly as it was."""
    import pytest

    from lumen.services.llm import send_message_stream

    def boom(*a, **kw):
        raise RuntimeError("prometheus is unhappy")

    with app.app_context():
        monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
        stream = send_message_stream(
            [{"role": "user", "content": "hi"}], test_model["model_name"],
            entity_id=test_user["id"], source="chat",
        )
        with pytest.raises(RuntimeError, match="No healthy endpoints"):
            next(stream)
