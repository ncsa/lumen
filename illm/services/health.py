import time
import threading
from datetime import datetime


def start_health_checker(app):
    """Start a background daemon thread that checks all endpoints every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    import openai
                    from illm.models.model_endpoint import ModelEndpoint
                    from illm.extensions import db

                    endpoints = ModelEndpoint.query.all()
                    for ep in endpoints:
                        try:
                            client = openai.OpenAI(api_key=ep.api_key, base_url=ep.url)
                            models = client.models.list()
                            model_ids = {m.id for m in models.data}
                            expected = ep.model_name or ep.model_config.model_name
                            ep.healthy = expected in model_ids
                        except Exception:
                            ep.healthy = False
                        ep.last_checked_at = datetime.utcnow()
                    db.session.commit()
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
