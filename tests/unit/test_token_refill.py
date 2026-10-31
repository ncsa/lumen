"""Tests for refill_coin_balances — the per-tick logic of the coin refiller."""
from datetime import datetime, timedelta, timezone

import pytest
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


def test_refills_at_exact_one_hour_boundary(app, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=50,
                      last_refill_at=now - timedelta(hours=1))
        db.session.commit()

        assert refill_coin_balances(now=now) == 1
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 60.0  # 50 + 1*10
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
        # Bumped so the next pass does not re-select it.
        assert bal.last_refill_at == now


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
        # Bumped so the next pass does not re-select it.
        assert bal.last_refill_at == now


def test_no_pool_skipped(app, test_user):
    """Entity with no limit at all (no EntityLimit, no group limits) gets no credit."""
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
        # Bumped so the next pass does not re-select it.
        assert bal.last_refill_at == now


def test_blocked_pool_skipped(app, test_user):
    """A blocked entity (max_coins == 0) gets no credit, but last_refill_at is bumped."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=0, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=5,
                      last_refill_at=now - timedelta(hours=3))
        db.session.commit()

        assert refill_coin_balances(now=now) == 0
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 5.0
        assert bal.last_refill_at == now


def test_skipped_balance_not_reselected_on_next_pass(app, test_user):
    """Once bumped, a skipped balance is not due again a minute later."""
    from unittest.mock import patch

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services import token_refill
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_balance(db, test_user["id"], coins_left=42,
                      last_refill_at=now - timedelta(hours=5))
        db.session.commit()

        assert token_refill.refill_coin_balances(now=now) == 0

        # The second pass finds nothing due, so it never reaches the limit lookups.
        with patch.object(token_refill, "best_group_pool_limit") as resolve:
            assert token_refill.refill_coin_balances(now=now + timedelta(minutes=1)) == 0
        resolve.assert_not_called()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one()
        assert float(bal.coins_left) == 42.0
        assert bal.last_refill_at == now


def test_skip_bump_guarded_on_value_read(app, test_user):
    """A skipped row whose last_refill_at changed after the read is not bumped."""
    from unittest.mock import patch

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services import token_refill
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        moved = now - timedelta(hours=2)
        _add_balance(db, test_user["id"], coins_left=42,
                      last_refill_at=now - timedelta(hours=5))
        db.session.commit()

        real_resolve = token_refill.best_group_pool_limit
        stmt = select(EntityBalance).filter_by(entity_id=test_user["id"])

        def move_then_resolve(*args, **kwargs):
            # Another writer changes last_refill_at between the read and the bump.
            db.session.execute(
                token_refill.sa_update(EntityBalance)
                .where(EntityBalance.entity_id == test_user["id"])
                .values(last_refill_at=moved)
                .execution_options(synchronize_session=False)
            )
            return real_resolve(*args, **kwargs)

        with patch.object(token_refill, "best_group_pool_limit", side_effect=move_then_resolve):
            assert token_refill.refill_coin_balances(now=now) == 0

        db.session.expire_all()
        bal = db.session.execute(stmt).scalar_one()
        assert bal.last_refill_at == moved


def test_partial_hour_uses_float(app, test_user):
    """1.7 hours elapsed → refill = 1.7*refresh_coins (no truncation)."""
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
        assert float(bal.coins_left) == pytest.approx(17.0)  # 1.7 * 10 = 17


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
    """subtract_coins must stamp last_refill_at so the new balance is picked up by the refiller."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        from lumen.services.token_refill import refill_coin_balances
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=10, starting_coins=50))
        db.session.commit()

        subtract_coins(entity_id, model_id, 1.0)
        db.session.commit()

        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert bal is not None
        assert bal.last_refill_at is not None, "last_refill_at must be set so the refiller can process this balance"

        # Simulate 2 hours passing — the balance should now be refillable.
        now = bal.last_refill_at + timedelta(hours=2)
        updated = refill_coin_balances(now=now)
        assert updated == 1


def test_default_pool_is_refilled(app, test_user):
    """Users on the global default pool (defaults.tokens) — no EntityLimit, no group limit —
    must be refilled from TOKEN_DEFAULTS (CODE-H1)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances
        app.config["TOKEN_DEFAULTS"] = {"max": 100, "refresh": 10, "starting": 50}
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            _add_balance(db, test_user["id"], coins_left=0,
                          last_refill_at=now - timedelta(hours=3))
            db.session.commit()

            assert refill_coin_balances(now=now) == 1
            bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
            assert float(bal.coins_left) == 30.0  # 0 + 3*10
        finally:
            app.config["TOKEN_DEFAULTS"] = None


def test_refill_does_not_clobber_concurrent_deduction(app, test_user):
    """Refill credits against the live DB balance, so a deduction that lands between the
    refiller's read and its write is preserved rather than erased (SQL-1)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services import token_refill

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=1000, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=100,
                      last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        # Simulate a concurrent atomic deduction of 50 after the refiller has read the
        # balance (100) but before it writes: patch _least to deduct mid-computation.
        real_least = token_refill._least
        stmt = select(EntityBalance).filter_by(entity_id=test_user["id"])

        def deduct_then_least(*args, **kwargs):
            bal = db.session.execute(stmt).scalar_one()
            bal.coins_left = float(bal.coins_left) - 50
            db.session.flush()
            token_refill._least = real_least  # only interfere once
            return real_least(*args, **kwargs)

        token_refill._least = deduct_then_least
        try:
            token_refill.refill_coin_balances(now=now)
        finally:
            token_refill._least = real_least

        bal = db.session.execute(stmt).scalar_one()
        # 100 - 50 (concurrent spend) + 2*10 (refill) = 70; the spend is NOT lost.
        assert float(bal.coins_left) == 70.0


def test_refill_reads_database_value_not_identity_map(app, test_user):
    """The credit follows last_refill_at in the database, not a stale in-memory ORM copy."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services.token_refill import refill_coin_balances

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=50,
                      last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        # An older value on the identity-map object only. Writing through __dict__
        # keeps it out of the dirty state, so it is never flushed to the row.
        bal = db.session.execute(
            select(EntityBalance).filter_by(entity_id=test_user["id"])
        ).scalar_one()
        vars(bal)["last_refill_at"] = now - timedelta(hours=5)

        assert refill_coin_balances(now=now) == 1

        db.session.expire_all()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one()
        assert float(bal.coins_left) == 70.0  # 50 + 2*10 from the database value, not 5*10
        assert bal.last_refill_at == now


def _add_entities(db, count):
    from lumen.models.entity import Entity
    entities = [
        Entity(entity_type="user", email=f"refill-{i}@example.com",
               name=f"Refill {i}", initials="RF", active=True)
        for i in range(count)
    ]
    db.session.add_all(entities)
    db.session.flush()
    return [e.id for e in entities]


def test_refill_commits_per_batch(app, monkeypatch):
    """Due rows are written in REFILL_BATCH_SIZE chunks, each committed on its own."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services import token_refill

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ids = _add_entities(db, 5)
        for eid in ids:
            _add_limit(db, eid, max_coins=100, refresh_coins=10)
            _add_balance(db, eid, coins_left=0, last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        monkeypatch.setattr(token_refill, "REFILL_BATCH_SIZE", 2)
        commits = []
        real_commit = db.session.commit
        monkeypatch.setattr(db.session, "commit", lambda: (commits.append(1), real_commit())[1])

        assert token_refill.refill_coin_balances(now=now) == 5
        assert len(commits) == 3  # 2 + 2 + 1

        db.session.expire_all()
        for eid in ids:
            bal = db.session.execute(select(EntityBalance).filter_by(entity_id=eid)).scalar_one()
            assert float(bal.coins_left) == 20.0
            assert bal.last_refill_at == now


def test_failed_batch_is_rolled_back_and_pass_continues(app, monkeypatch):
    """One failing batch does not stop the batches after it."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services import token_refill

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ids = _add_entities(db, 3)
        for eid in ids:
            _add_limit(db, eid, max_coins=100, refresh_coins=10)
            _add_balance(db, eid, coins_left=0, last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        monkeypatch.setattr(token_refill, "REFILL_BATCH_SIZE", 1)
        real_apply = token_refill._apply_batch

        def fail_first(stmt, params):
            if params[0]["b_entity_id"] == ids[0]:
                db.session.execute(stmt, params)  # written, then lost to the rollback
                raise RuntimeError("batch failed")
            return real_apply(stmt, params)

        monkeypatch.setattr(token_refill, "_apply_batch", fail_first)
        assert token_refill.refill_coin_balances(now=now) == 2

        db.session.expire_all()
        coins = {
            eid: float(db.session.execute(select(EntityBalance).filter_by(entity_id=eid)).scalar_one().coins_left)
            for eid in ids
        }
        assert coins == {ids[0]: 0.0, ids[1]: 20.0, ids[2]: 20.0}


def test_refill_guarded_on_value_read(app, test_user, monkeypatch):
    """A due row whose last_refill_at changed after the read is neither credited nor bumped."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.services import token_refill

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Still due after the move, so only the exact-value guard (not a due cutoff) rejects it.
        moved = now - timedelta(minutes=90)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=50,
                      last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        real_apply = token_refill._apply_batch

        def move_then_apply(stmt, params):
            # An admin reset or another worker lands between the read and the write.
            db.session.execute(
                token_refill.sa_update(EntityBalance.__table__)
                .where(EntityBalance.__table__.c.entity_id == test_user["id"])
                .values(last_refill_at=moved)
            )
            return real_apply(stmt, params)

        monkeypatch.setattr(token_refill, "_apply_batch", move_then_apply)
        assert token_refill.refill_coin_balances(now=now) == 0

        db.session.expire_all()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one()
        assert float(bal.coins_left) == 50.0
        assert bal.last_refill_at == moved


def test_sqlite_pass_takes_no_advisory_lock(app, test_user):
    """SQLite has no advisory locks: the pass runs without trying to take one."""
    from sqlalchemy import event
    with app.app_context():
        from lumen.extensions import db
        from lumen.services import token_refill
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_limit(db, test_user["id"], max_coins=100, refresh_coins=10)
        _add_balance(db, test_user["id"], coins_left=50, last_refill_at=now - timedelta(hours=2))
        db.session.commit()

        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", record)
        try:
            assert token_refill.refill_coin_balances(now=now) == 1
        finally:
            event.remove(db.engine, "before_cursor_execute", record)

        assert statements
        assert not [s for s in statements if "advisory" in s]


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
