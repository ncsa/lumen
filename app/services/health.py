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
                    from app.models.model_endpoint import ModelEndpoint
                    from app.extensions import db

                    endpoints = ModelEndpoint.query.all()
                    for ep in endpoints:
                        try:
                            client = openai.OpenAI(api_key=ep.api_key, base_url=ep.url)
                            client.models.list()
                            ep.healthy = True
                        except Exception:
                            ep.healthy = False
                        ep.last_checked_at = datetime.utcnow()
                    db.session.commit()
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
