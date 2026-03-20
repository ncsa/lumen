import time
import threading
from datetime import datetime


def start_token_refiller(app):
    """Start a background daemon thread that refills token budgets every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    from illm.models.entity_model_balance import EntityModelBalance
                    from illm.extensions import db
                    from illm.services.llm import get_effective_limit

                    now = datetime.utcnow()
                    balances = EntityModelBalance.query.filter(
                        EntityModelBalance.last_refill_at != None  # noqa: E711
                    ).all()
                    for bal in balances:
                        hours_elapsed = (now - bal.last_refill_at).total_seconds() / 3600
                        if hours_elapsed >= 1:
                            effective = get_effective_limit(bal.entity_id, bal.model_config_id)
                            if effective is None:
                                continue
                            max_tokens, refresh_tokens, _starting = effective
                            if max_tokens == -2 or refresh_tokens <= 0:
                                continue
                            refill = int(hours_elapsed) * refresh_tokens
                            bal.tokens_left = min(max_tokens, bal.tokens_left + refill)
                            bal.last_refill_at = now
                    db.session.commit()
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
