import concurrent.futures
import logging
import time
import threading

import openai
from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from lumen.extensions import db
from lumen.timeutils import utcnow
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint

logger = logging.getLogger(__name__)

# The openai/httpx client's own timeout=5.0 bounds well-behaved failures (refused,
# reset), but a network path that silently drops packets (no RST) can leave the
# raw socket.connect() blocked far longer than that. Running each probe through
# this executor with a hard result() timeout guarantees one blackholed endpoint
# can't stall every later endpoint in the same pass. A stuck probe's thread is
# abandoned (not killed — Python can't force-abort a blocked native call) and
# occupies a worker slot until the OS eventually gives up; the pool is sized
# with headroom so that's tolerable unless many endpoints are blackholed at once.
_PROBE_TIMEOUT = 10
_probe_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="health-probe")


def _probe_endpoint(url: str, api_key: str, expected: str) -> bool:
    with openai.OpenAI(api_key=api_key, base_url=url, timeout=5.0) as client:
        models = client.models.list()
    return expected in {m.id for m in models.data}


def check_all_endpoints() -> int:
    """Run one health-check pass for all endpoints. Caller owns the app context.
    Returns the number of endpoints checked."""
    log_enabled = current_app.config.get("LOG_MODEL_HEALTH", False)
    # Join model_name upfront to avoid lazy-loading ep.model_config inside the loop.
    # Accessing the backref on a lazy="dynamic" + delete-orphan relationship can cause
    # SQLAlchemy to schedule the endpoint for deletion, producing StaleDataError on commit.
    rows = db.session.execute(
        select(ModelEndpoint, ModelConfig.model_name.label("config_model_name"))
        .join(ModelConfig, ModelEndpoint.model_config_id == ModelConfig.id)
    ).all()
    # Capture every scalar needed for probing/logging before releasing the read
    # transaction. Each probe is a network call (up to 5s + retries per endpoint),
    # so holding the transaction open across the loop would leave the connection
    # idle-in-transaction and trip Postgres's idle_in_transaction_session_timeout.
    probes = [
        (ep, ep.url, ep.api_key, ep.model_name or config_model_name)
        for ep, config_model_name in rows
    ]
    db.session.rollback()  # end the read transaction before the slow network probes

    now = utcnow()
    for ep, url, api_key, expected in probes:
        try:
            ep.healthy = _probe_executor.submit(_probe_endpoint, url, api_key, expected).result(timeout=_PROBE_TIMEOUT)
            if log_enabled:
                found = "found" if ep.healthy else "NOT FOUND"
                current_app.logger.info(
                    "health check %s → endpoint UP, model '%s' %s", url, expected, found
                )
        except concurrent.futures.TimeoutError:
            ep.healthy = False
            if log_enabled:
                current_app.logger.info(
                    "health check %s → endpoint DOWN (probe exceeded %ss, abandoned)", url, _PROBE_TIMEOUT
                )
        except Exception as e:
            ep.healthy = False
            if log_enabled:
                cause = e.__cause__ or e
                current_app.logger.info(
                    "health check %s → endpoint DOWN (%r)", url, cause
                )
        ep.last_checked_at = now
    try:
        db.session.commit()
    except StaleDataError:
        # An endpoint was deleted between the SELECT and the commit; discard stale updates.
        db.session.rollback()
        logger.warning("health check: endpoint deleted mid-pass, changes discarded")
    return len(probes)


def start_health_checker(app):
    """Start a background daemon thread that checks all endpoints every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    check_all_endpoints()
            except Exception:
                logger.exception("health checker error")
            time.sleep(60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
