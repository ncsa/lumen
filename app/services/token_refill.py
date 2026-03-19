import time
import threading
from datetime import datetime


def start_token_refiller(app):
    """Start a background daemon thread that refills token budgets every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    from app.models.model_limit import ModelLimit
                    from app.extensions import db

                    now = datetime.utcnow()
                    limits = ModelLimit.query.filter(
                        ModelLimit.tokens_per_hour > 0,
                        ModelLimit.token_limit > 0,
                    ).all()
                    for limit in limits:
                        if limit.last_refill_at is None:
                            continue
                        hours_elapsed = (now - limit.last_refill_at).total_seconds() / 3600
                        if hours_elapsed >= 1:
                            refill = int(hours_elapsed) * limit.tokens_per_hour
                            limit.tokens_left = min(limit.token_limit, limit.tokens_left + refill)
                            limit.last_refill_at = now
                    db.session.commit()
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
