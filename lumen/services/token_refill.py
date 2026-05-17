import logging
import time
import threading
from datetime import datetime, timezone

from sqlalchemy import select

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_limit import GroupLimit
from lumen.services.llm import PoolLimit

logger = logging.getLogger(__name__)


def refill_coin_balances(now: datetime = None) -> int:
    """Run one refill pass; return the number of balances updated. Caller owns the app context."""
    if now is None:
        now = datetime.now(timezone.utc)
    balances = db.session.execute(
        select(EntityBalance).where(EntityBalance.last_refill_at != None)  # noqa: E711
    ).scalars().all()

    due = [bal for bal in balances if (now - bal.last_refill_at).total_seconds() / 3600 >= 1]
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

    updated = 0
    for bal in due:
        eid = bal.entity_id
        hours_elapsed = (now - bal.last_refill_at).total_seconds() / 3600

        if eid in entity_limits:
            el = entity_limits[eid]
            if float(el.max_coins) == 0:
                continue
            pool = PoolLimit(float(el.max_coins), float(el.refresh_coins), float(el.starting_coins))
        else:
            gids = group_ids_by_entity.get(eid, [])
            if not gids:
                continue
            candidates = [
                PoolLimit(float(gl.max_coins), float(gl.refresh_coins), float(gl.starting_coins))
                for gid in gids
                for gl in group_limits_by_group.get(gid, [])
                if float(gl.max_coins) != 0
            ]
            if not candidates:
                continue
            if any(c.max_coins == -2 for c in candidates):
                pool = PoolLimit(-2, 0, 0)
            else:
                pool = max(candidates, key=lambda x: x.max_coins)

        max_coins, refresh_coins, _starting = pool
        if max_coins == -2 or refresh_coins <= 0:
            continue
        refill = int(hours_elapsed) * float(refresh_coins)
        bal.coins_left = min(max_coins, float(bal.coins_left) + refill)
        bal.last_refill_at = now
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
