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
                    from app.services.llm import get_effective_limit

                    now = datetime.utcnow()
                    # Only per-model rows carry tokens_left state (model_config_id IS NOT NULL)
                    limits = ModelLimit.query.filter(
                        ModelLimit.model_config_id != None,  # noqa: E711
                    ).all()
                    for limit in limits:
                        if limit.last_refill_at is None:
                            continue
                        hours_elapsed = (now - limit.last_refill_at).total_seconds() / 3600
                        if hours_elapsed >= 1:
                            effective = get_effective_limit(limit.entity_id, limit.model_config_id)
                            if effective is None:
                                continue
                            max_tokens, refresh_tokens, _starting = effective
                            if max_tokens == -2 or refresh_tokens <= 0:
                                continue
                            refill = int(hours_elapsed) * refresh_tokens
                            limit.tokens_left = min(max_tokens, limit.tokens_left + refill)
                            limit.last_refill_at = now
                    db.session.commit()
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
