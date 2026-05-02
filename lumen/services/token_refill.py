import time
import threading
from datetime import datetime, timezone

from lumen.extensions import db
from lumen.models.entity_balance import EntityBalance
from lumen.services.llm import get_pool_limit


def refill_coin_balances(now: datetime = None) -> int:
    """Run one refill pass; return the number of balances updated. Caller owns the app context."""
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    balances = EntityBalance.query.filter(
        EntityBalance.last_refill_at != None  # noqa: E711
    ).all()
    updated = 0
    for bal in balances:
        hours_elapsed = (now - bal.last_refill_at).total_seconds() / 3600
        if hours_elapsed < 1:
            continue
        pool = get_pool_limit(bal.entity_id)
        if pool is None:
            continue
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
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
