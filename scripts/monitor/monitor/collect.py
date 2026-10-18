"""The price run: Lumen prices, the models.dev range and hardware cost.

Each run re-reads the config and overwrites today's rows with the latest
values, so running it several times a day keeps one row per model and source.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from monitor import hardware, lumen_api, modelsdev
from monitor.config import Config, ConfigStore
from monitor.daily import get_daily, local_day, upsert_daily
from monitor.db import utcnow

log = logging.getLogger(__name__)


def _avg_healthy(session, day, model: dict) -> float:
    """Average healthy instances today from the probe counters, or the current count before any probe."""
    row = get_daily(session, day, model["id"], "lumen")
    if row is not None and row.polls:
        return row.healthy_sum / row.polls
    return float(model["healthy"] or 0)


def _hardware_values(config: Config, model_id: str, avg_healthy: float) -> dict | None:
    hw = config.hardware_for(model_id)
    if hw is None:
        return None
    gpu, gpus = hw
    return {
        "daily_cost": hardware.daily_cost(gpu, gpus, config.kwh, avg_healthy),
        "gpu": config.models[model_id].gpu,
        "gpus": gpus,
        "gpu_price": gpu.price,
        "energy": gpu.energy,
        "kwh": config.kwh,
        "avg_healthy": avg_healthy,
    }


def collect_prices(store: ConfigStore, session_factory: sessionmaker, now: datetime | None = None) -> dict:
    """Upsert today's lumen, models.dev and hardware rows; returns the number of rows per source.

    Raises when Lumen cannot be read, since its model list drives the run.
    A models.dev failure only skips the models.dev rows.
    """
    store.reload()
    config = store.current
    timeout = config.refresh.timeout.total_seconds()
    day = local_day(now or utcnow(), config.timezone)

    models = lumen_api.fetch_models(config.lumen_url, config.lumen_api_key, timeout)
    dev_models = modelsdev.fetch_modelsdev(timeout)
    if not dev_models:
        log.warning("models.dev returned no models; skipping the models.dev rows")
    price_index = modelsdev.build_price_index(dev_models)

    written = {"lumen": 0, "models.dev": 0, "hardware": 0}
    with session_factory() as session:
        for model in models:
            model_id = model["id"]
            upsert_daily(session, day, model_id, "lumen", input_price=model["input_price"], output_price=model["output_price"])
            written["lumen"] += 1

            entry = config.models.get(model_id)
            dev_match = modelsdev.match(model_id, model["root"], dev_models, pin=entry.modelsdev_id if entry else None)
            stats = modelsdev.provider_stats(dev_match, price_index) if dev_match else None
            if stats:
                upsert_daily(session, day, model_id, "models.dev", **stats)
                written["models.dev"] += 1

            values = _hardware_values(config, model_id, _avg_healthy(session, day, model))
            if values:
                upsert_daily(session, day, model_id, "hardware", **values)
                written["hardware"] += 1
        session.commit()
    log.info("price run for %s: %s", day, written)
    return written
