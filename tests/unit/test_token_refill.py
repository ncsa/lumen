"""Tests for refill_coin_balances — the per-tick logic of the coin refiller."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select


def _add_balance(db, entity_id, coins_left, last_refill_at):
    from lumen.models.entity_balance import EntityBalance
    bal = EntityBalance(
        entity_id=entity_id,
        coins_left=coins_left,
        last_refill_at=last_refill_at,
    )
    db.session.add(bal)
    db.session.flush()
    return bal


def _add_limit(db, entity_id, max_coins, refresh_coins, starting_coins=0):
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(
        entity_id=entity_id,
        max_coins=max_coins,
        refresh_coins=refresh_coins,
        starting_coins=starting_coins,
    ))


def test_no_balances_returns_zero(app):
    with app.app_context():
        from lumen.services.token_refill import refill_coin_balances
        assert refill_coin_balances() == 0


def test_skips_balance_younger_than_one_hour(app, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        bal = _add_balance(db, test_user["id"], coins_left=50,
                            last_refill_at=now - timedelta(minutes=30))
        db.session.commit()

        updated = refill_coin_balances(now=now)
        assert updated == 0
        # Balance unchanged
        from lumen.models.entity_balance import EntityBalance
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 50.0


def test_refills_after_full_hour(app, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=50,
                      last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        assert refill_coin_balances(now=now) == 1
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 70.0  # 50 + 2*10
        # last_refill_at advanced
        assert bal.last_refill_at == now


def test_refill_capped_at_max_coins(app, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=50)
        _add_balance(db, test_user["id"], coins_left=80,
                      last_refill_at=now - timedelta(hours=5))
        db.session.commit()

        refill_coin_balances(now=now)
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        # 80 + 5*50 = 330; capped at 100
        assert float(bal.coins_left) == 100.0


def test_unlimited_pool_skipped(app, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=-2, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=0,
                      last_refill_at=now - timedelta(hours=10))
        db.session.commit()

        assert refill_coin_balances(now=now) == 0
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 0.0


def test_zero_refresh_coins_skipped(app, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=0)
        _add_balance(db, test_user["id"], coins_left=20,
                      last_refill_at=now - timedelta(hours=10))
        db.session.commit()

        assert refill_coin_balances(now=now) == 0
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 20.0


def test_no_pool_skipped(app, test_user):
    """Entity with no limit at all (no EntityLimit, no group limits) is left alone."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_balance(db, test_user["id"], coins_left=42,
                      last_refill_at=now - timedelta(hours=5))
        db.session.commit()

        assert refill_coin_balances(now=now) == 0
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 42.0


def test_partial_hour_uses_floor(app, test_user):
    """1.7 hours elapsed → refill = 1*refresh_coins, not 1.7*."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=1000, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=0,
                      last_refill_at=now - timedelta(hours=1, minutes=42))
        db.session.commit()

        refill_coin_balances(now=now)
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 10.0  # int(1.7) * 10 = 10


def test_skips_balance_with_null_last_refill_at(app, test_user):
    """EntityBalance rows with last_refill_at=None are excluded from the refill query.
    In practice this state should not arise because both EntityBalance creation sites now
    set last_refill_at to the current time — but the guard is still tested here."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        db.session.add(EntityBalance(entity_id=test_user["id"], coins_left=50, last_refill_at=None))
        db.session.commit()

        assert refill_coin_balances(now=now) == 0
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 50.0


def test_new_balance_created_with_last_refill_at(app, test_user, test_model):
    """get_coin_balance must stamp last_refill_at so the new balance is picked up by the refiller."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import get_coin_balance
        from lumen.services.token_refill import refill_coin_balances
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=10, starting_coins=50))
        db.session.commit()

        get_coin_balance(entity_id, model_id)
        db.session.commit()

        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert bal is not None
        assert bal.last_refill_at is not None, "last_refill_at must be set so the refiller can process this balance"

        # Simulate 2 hours passing — the balance should now be refillable.
        now = bal.last_refill_at + timedelta(hours=2)
        updated = refill_coin_balances(now=now)
        assert updated == 1


def test_start_coin_refiller_starts_daemon_thread(app):
    """start_coin_refiller must start exactly one daemon thread (covers lines 41-51)."""
    import threading
    from unittest.mock import patch

    captured = []
    original_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        t = original_thread(*args, **kwargs)
        captured.append(t)
        return t

    with patch("lumen.services.token_refill.threading.Thread", side_effect=fake_thread):
        from lumen.services.token_refill import start_coin_refiller
        start_coin_refiller(app)

    assert len(captured) == 1
    assert captured[0].daemon is True
