import logging
import os
from functools import wraps
from http import HTTPStatus

from flask import Blueprint, Response, current_app, request
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

logger = logging.getLogger(__name__)
metrics_bp = Blueprint("metrics", __name__)


def _metrics_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        yaml_data = current_app.config.get("YAML_DATA", {})
        prom_cfg = yaml_data.get("api", {}).get("prometheus", {})
        if not prom_cfg.get("enabled", False):
            return Response("Not found", status=HTTPStatus.NOT_FOUND)
        token = prom_cfg.get("token", "")
        if not token:
            return Response("Unauthorized", status=HTTPStatus.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
        import hmac as _hmac
        auth = request.headers.get("Authorization", "")
        bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not bearer or not _hmac.compare_digest(bearer, token):
            return Response("Unauthorized", status=HTTPStatus.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
        return f(*args, **kwargs)
    return decorated


class LumenDBCollector:
    """Custom Prometheus collector that queries the DB on each scrape."""

    def collect(self):
        from sqlalchemy import func, select
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.models.model_stat import ModelStat
        from lumen.models.entity import Entity

        rows = db.session.execute(
            select(
                ModelConfig.model_name,
                ModelStat.source,
                func.coalesce(func.sum(ModelStat.requests), 0),
                func.coalesce(func.sum(ModelStat.input_tokens), 0),
                func.coalesce(func.sum(ModelStat.output_tokens), 0),
                func.coalesce(func.sum(ModelStat.cost), 0),
            )
            .join(ModelConfig, ModelStat.model_config_id == ModelConfig.id)
            .group_by(ModelConfig.model_name, ModelStat.source)
        ).all()

        # Cumulative per-(model, source) usage totals summed from ModelStat.
        # CounterMetricFamily appends "_total" to each name, so the exposed
        # samples are lumen_model_requests_total, lumen_model_cost_coins_total, etc.
        reqs_m = CounterMetricFamily(
            "lumen_model_requests",
            "Cumulative LLM requests per model and source",
            labels=["model", "source"],
        )
        inp_m = CounterMetricFamily(
            "lumen_model_input_tokens",
            "Cumulative input tokens per model and source",
            labels=["model", "source"],
        )
        out_m = CounterMetricFamily(
            "lumen_model_output_tokens",
            "Cumulative output tokens per model and source",
            labels=["model", "source"],
        )
        cost_m = CounterMetricFamily(
            "lumen_model_cost_coins",
            "Cumulative cost in coins per model and source",
            labels=["model", "source"],
        )

        for model_name, source, reqs, inp, out, cost in rows:
            labels = [model_name, source]
            reqs_m.add_metric(labels, float(reqs))
            inp_m.add_metric(labels, float(inp))
            out_m.add_metric(labels, float(out))
            cost_m.add_metric(labels, float(cost))

        yield reqs_m
        yield inp_m
        yield out_m
        yield cost_m

        health_m = GaugeMetricFamily(
            "lumen_model_endpoint_healthy",
            "1=healthy 0=unhealthy per model endpoint",
            labels=["model", "endpoint_url"],
        )
        for model_name, url, healthy in db.session.execute(
            select(ModelConfig.model_name, ModelEndpoint.url, ModelEndpoint.healthy)
            .join(ModelConfig, ModelEndpoint.model_config_id == ModelConfig.id)
        ).all():
            health_m.add_metric([model_name, url], 1.0 if healthy else 0.0)
        yield health_m

        # active = admin has not disabled the user (Entity.active=True, the default)
        # total  = all users ever registered
        users_m = GaugeMetricFamily(
            "lumen_users",
            "User counts: active (not disabled by admin) and total",
            labels=["status"],
        )
        active_count = db.session.scalar(
            select(func.count(Entity.id)).filter_by(entity_type="user", active=True)
        ) or 0
        total_count = db.session.scalar(
            select(func.count(Entity.id)).filter_by(entity_type="user")
        ) or 0
        users_m.add_metric(["active"], float(active_count))
        users_m.add_metric(["total"], float(total_count))
        yield users_m

        # Connection-pool gauges. A slow leak (connections checked out and never
        # returned) shows up here as checked_out climbing and never falling back;
        # correlate the climb with the access log to find the leaking endpoint.
        pool = db.engine.pool
        pool_m = GaugeMetricFamily(
            "lumen_db_pool_connections",
            "SQLAlchemy connection-pool state",
            labels=["state"],
        )
        try:
            size = pool.size()
            checked_out = pool.checkedout()
            limit = size + pool._max_overflow if pool._max_overflow >= 0 else None
            pool_m.add_metric(["size"], float(size))
            pool_m.add_metric(["checked_in"], float(pool.checkedin()))
            pool_m.add_metric(["checked_out"], float(checked_out))
            pool_m.add_metric(["overflow"], float(pool.overflow()))
            if limit:
                pool_m.add_metric(["limit"], float(limit))
                if checked_out >= 0.8 * limit:
                    logger.warning(
                        "DB pool near capacity: %d/%d connections checked out",
                        checked_out, limit,
                    )
        except AttributeError:
            # Pools without queue semantics (SQLite StaticPool/NullPool) lack these.
            pass
        yield pool_m


@metrics_bp.route("/metrics")
@_metrics_auth_required
def metrics():
    # DB metrics — always globally accurate, queries the shared database
    db_output = generate_latest(current_app.config["PROMETHEUS_REGISTRY"])

    # HTTP metrics — use multiprocess aggregation if PROMETHEUS_MULTIPROC_DIR is set
    # (all workers write to shared dir; any worker can serve the full aggregate),
    # otherwise fall back to the default per-process registry.
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        from prometheus_client.multiprocess import MultiProcessCollector
        mp_registry = CollectorRegistry()
        MultiProcessCollector(mp_registry)
        http_output = generate_latest(mp_registry)
    else:
        from prometheus_client import REGISTRY
        http_output = generate_latest(REGISTRY)

    return Response(db_output + http_output, status=HTTPStatus.OK, mimetype=CONTENT_TYPE_LATEST)
