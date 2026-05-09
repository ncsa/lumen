import time
import threading
from datetime import datetime, timezone

import openai
from flask import current_app
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.model_endpoint import ModelEndpoint


def check_all_endpoints() -> int:
    """Run one health-check pass for all endpoints. Caller owns the app context.
    Returns the number of endpoints checked."""
    log_enabled = current_app.config.get("LOG_MODEL_HEALTH", False)
    endpoints = db.session.execute(select(ModelEndpoint)).scalars().all()
    for ep in endpoints:
        try:
            with openai.OpenAI(api_key=ep.api_key, base_url=ep.url) as client:
                models = client.models.list()
            model_ids = {m.id for m in models.data}
            expected = ep.model_name or ep.model_config.model_name
            ep.healthy = expected in model_ids
            if log_enabled:
                found = "found" if ep.healthy else "NOT FOUND"
                current_app.logger.info(
                    "health check %s → endpoint UP, model '%s' %s", ep.url, expected, found
                )
        except Exception as e:
            ep.healthy = False
            if log_enabled:
                cause = e.__cause__ or e
                current_app.logger.info(
                    "health check %s → endpoint DOWN (%r)", ep.url, cause
                )
        ep.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    return len(endpoints)


def start_health_checker(app):
    """Start a background daemon thread that checks all endpoints every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    check_all_endpoints()
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
