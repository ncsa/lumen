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
