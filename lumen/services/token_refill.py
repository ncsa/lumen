import logging
import threading
import time
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import select
from sqlalchemy import update as sa_update

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.services.llm import PoolLimit, _least, best_group_pool_limit
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)


def refill_coin_balances(now: datetime = None) -> int:
    """Run one refill pass; return the number of balances updated. Caller owns the app context."""
    if now is None:
        now = utcnow()
    elif now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    one_hour_ago = now - timedelta(hours=1)
    due = db.session.execute(
        select(EntityBalance).where(
            EntityBalance.last_refill_at != None,  # noqa: E711
            EntityBalance.last_refill_at <= one_hour_ago,
        )
    ).scalars().all()
    if not due:
        return 0

    entity_ids = [bal.entity_id for bal in due]

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

    updated = 0
    for bal in due:
        eid = bal.entity_id
        # last_refill_at is naive UTC; tolerate a stray aware value so one bad row
        # can't abort the whole pass.
        last_refill = bal.last_refill_at
        if last_refill.tzinfo is not None:
            last_refill = last_refill.replace(tzinfo=None)
        hours_elapsed = (now - last_refill).total_seconds() / 3600

        if eid in entity_limits:
            el = entity_limits[eid]
            if float(el.max_coins) == 0:
                continue
            pool = PoolLimit(float(el.max_coins), float(el.refresh_coins), float(el.starting_coins))
        else:
            gids = group_ids_by_entity.get(eid, [])
            group_limits = [gl for gid in gids for gl in group_limits_by_group.get(gid, [])]
            pool = best_group_pool_limit(group_limits)
            if pool is None:
                pool = default_pool
            if pool is None:
                continue

        max_coins, refresh_coins, _starting = pool
        if max_coins == -2 or refresh_coins <= 0:
            continue
        refill = hours_elapsed * float(refresh_coins)
        # Atomic set-based credit against the live DB balance so a concurrent atomic
        # deduction (subtract_coins) is never clobbered by a stale read-modify-write.
        # The last_refill_at cutoff makes a second worker that already refilled a no-op.
        result = db.session.execute(
            sa_update(EntityBalance)
            .where(
                EntityBalance.entity_id == eid,
                EntityBalance.last_refill_at <= one_hour_ago,
            )
            .values(
                coins_left=_least(float(max_coins), EntityBalance.coins_left + refill),
                last_refill_at=now,
            )
        )
        if result.rowcount:
            updated += 1
    db.session.commit()
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
