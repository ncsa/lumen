import logging
import threading
import time
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import Float, bindparam, select, text
from sqlalchemy import update as sa_update

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.services.llm import REFILL_INTERVAL, PoolLimit, _least, best_group_pool_limit
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)


#: How many balances one refill transaction writes. Each batch commits on its own,
#: so a billing request waits at most for one batch's row locks, not a whole pass.
REFILL_BATCH_SIZE = 200

#: PostgreSQL advisory lock key held for a whole refill pass, so one replica
#: refills at a time. Any fixed bigint no other code in the database uses.
_REFILL_LOCK_KEY = 7_172_093_405_172

_balances = EntityBalance.__table__


def _cas_update(**values):
    """UPDATE balances, one per param set, each guarded on the last_refill_at value read.

    A row an admin or another worker changed after the read is left alone. A Core
    statement on the table, so a guarded write that matches no row leaves the
    session's copy of the row untouched.
    """
    return (
        sa_update(_balances)
        .where(
            _balances.c.entity_id == bindparam("b_entity_id"),
            _balances.c.last_refill_at == bindparam("b_read"),
        )
        .values(last_refill_at=bindparam("b_now"), **values)
    )


def _apply_batch(stmt, params: list) -> int:
    """Write one batch as a single executemany and commit it; return rows matched."""
    result = db.session.execute(stmt, params)
    db.session.commit()
    return result.rowcount


def _apply_in_batches(stmt, params: list) -> int:
    """Send params through _apply_batch in chunks; a failed chunk is rolled back and skipped."""
    applied = 0
    for start in range(0, len(params), REFILL_BATCH_SIZE):
        batch = params[start:start + REFILL_BATCH_SIZE]
        try:
            applied += _apply_batch(stmt, batch)
        except Exception:
            db.session.rollback()
            logger.exception(
                "coin refill batch failed (entities %s..%s); continuing",
                batch[0]["b_entity_id"], batch[-1]["b_entity_id"],
            )
    return applied


def refill_coin_balances(now: datetime = None) -> int:
    """Run one refill pass; return the number of balances credited. Caller owns the app context.

    On PostgreSQL the pass runs under a session-level advisory lock, so while
    one replica refills, the others skip their pass and return 0.
    """
    if db.engine.dialect.name != "postgresql":
        return _refill_pass(now)
    # A connection of its own, committed right after the lock call: a
    # session-level lock outlives the transaction, so the connection sits
    # idle, not idle in transaction, while the batches commit on db.session.
    conn = db.engine.connect()
    try:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _REFILL_LOCK_KEY}
        ).scalar_one()
        conn.commit()
    except Exception:
        # The lock may be held without us knowing; dropping the connection frees it.
        conn.invalidate()
        conn.close()
        raise
    if not acquired:
        conn.close()
        return 0
    try:
        return _refill_pass(now)
    finally:
        _release_refill_lock(conn)


def _release_refill_lock(conn) -> None:
    """Unlock and close; if the unlock fails, drop the connection so the lock dies with it."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _REFILL_LOCK_KEY})
        conn.commit()
    except Exception:
        logger.exception("coin refill lock release failed; discarding its connection")
        conn.invalidate()
    finally:
        conn.close()


def _refill_pass(now: datetime = None) -> int:
    """The refill pass itself, with no lock; see refill_coin_balances."""
    if now is None:
        now = utcnow()
    elif now.tzinfo is not None:
        # Interpret an aware value as UTC and normalize instead of dropping the
        # offset, so a caller-passed non-UTC time cannot skew the refill window.
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    one_hour_ago = now - REFILL_INTERVAL
    # Column read, not ORM rows: last_refill_at comes from the database (always
    # naive), never from a stale identity-map object. Ordered so every batch
    # locks its rows in entity_id order.
    due = db.session.execute(
        select(EntityBalance.entity_id, EntityBalance.last_refill_at)
        .where(
            EntityBalance.last_refill_at != None,  # noqa: E711
            EntityBalance.last_refill_at <= one_hour_ago,
        )
        .order_by(EntityBalance.entity_id)
    ).all()
    if not due:
        return 0

    entity_ids = [eid for eid, _ in due]

    # Bulk-load entity limits to avoid N+1 per-entity queries
    entity_limits = {
        r.entity_id: r
        for r in db.session.execute(
            select(EntityLimit).where(EntityLimit.entity_id.in_(entity_ids))
        ).scalars().all()
    }

    no_entity_limit_ids = [eid for eid in entity_ids if eid not in entity_limits]
    group_ids_by_entity: dict = {}
    if no_entity_limit_ids:
        for m in db.session.execute(
            select(GroupMember)
            .join(Group, Group.id == GroupMember.group_id)
            .where(GroupMember.entity_id.in_(no_entity_limit_ids), Group.active == True)  # noqa: E712
        ).scalars().all():
            group_ids_by_entity.setdefault(m.entity_id, []).append(m.group_id)

    all_group_ids = {gid for gids in group_ids_by_entity.values() for gid in gids}
    group_limits_by_group: dict = {}
    if all_group_ids:
        for gl in db.session.execute(
            select(GroupLimit).where(GroupLimit.group_id.in_(all_group_ids))
        ).scalars().all():
            group_limits_by_group.setdefault(gl.group_id, []).append(gl)

    # Global default coin pool, used for entities with no entity- or group-level limit
    # (mirrors get_pool_limit's fallback so default-pool users get refilled too).
    td = current_app.config.get("TOKEN_DEFAULTS")
    default_pool = None
    if td and float(td["max"]) != 0:
        default_pool = PoolLimit(float(td["max"]), float(td["refresh"]), float(td["starting"]))

    credits = []
    bumps = []
    for eid, last_refill in due:
        params = {"b_entity_id": eid, "b_read": last_refill, "b_now": now}
        pool = None
        if eid in entity_limits:
            el = entity_limits[eid]
            if float(el.max_coins) != 0:
                pool = PoolLimit(float(el.max_coins), float(el.refresh_coins), float(el.starting_coins))
        else:
            gids = group_ids_by_entity.get(eid, [])
            group_limits = [gl for gid in gids for gl in group_limits_by_group.get(gid, [])]
            pool = best_group_pool_limit(group_limits)
            if pool is None:
                pool = default_pool

        if pool is None or pool.max_coins == -2 or pool.refresh_coins <= 0:
            bumps.append(params)
            continue

        max_coins, refresh_coins, _starting = pool
        hours_elapsed = (now - last_refill).total_seconds() / 3600
        params["b_max"] = float(max_coins)
        params["b_refill"] = hours_elapsed * float(refresh_coins)
        credits.append(params)

    # Atomic set-based credit against the live coins_left, so a concurrent atomic
    # deduction (subtract_coins) is never clobbered by a stale read-modify-write.
    credit = _cas_update(coins_left=_least(
        bindparam("b_max", type_=Float),
        _balances.c.coins_left + bindparam("b_refill", type_=Float),
    ))
    updated = _apply_in_batches(credit, credits)
    # Blocked, pool-less, unlimited or non-refreshing: no credit, but bump
    # last_refill_at so the row is not re-selected every pass.
    _apply_in_batches(_cas_update(), bumps)
    return updated


def start_coin_refiller(app):
    """Start a background daemon thread that refills coin budgets every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    refill_coin_balances()
            except Exception:
                logger.exception("coin refill error")
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
