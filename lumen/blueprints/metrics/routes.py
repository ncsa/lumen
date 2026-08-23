import logging
import os
import socket
import threading
import time
from functools import wraps
from http import HTTPStatus

from flask import Blueprint, Response, current_app, request
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from lumen.extensions import db
from lumen.services.ctx_probe import format_context_anomalies
from lumen.services.pool_tracker import (
    STRANDED_AFTER,
    format_holders,
    format_outstanding,
    format_scope_report,
    stranded_count,
    thread_dump,
    watchdog,
)

logger = logging.getLogger(__name__)
metrics_bp = Blueprint("metrics", __name__)


def _metrics_auth_required(*token_keys):
    """Require a bearer token from ``api.prometheus`` config.

    ``token_keys`` are tried in order; the first key with a value configured is
    the one the request must match. This lets /metrics/debug demand a separate,
    stronger ``debug_token`` when one is configured (it exposes thread dumps and
    deployment details) while falling back to the scrape ``token`` otherwise.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            yaml_data = current_app.config.get("YAML_DATA", {})
            prom_cfg = yaml_data.get("api", {}).get("prometheus", {})
            if not prom_cfg.get("enabled", False):
                return Response("Not found", status=HTTPStatus.NOT_FOUND)
            token = next((prom_cfg.get(k) for k in token_keys if prom_cfg.get(k)), "")
            if not token:
                return Response("Unauthorized", status=HTTPStatus.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
            import hmac as _hmac
            auth = request.headers.get("Authorization", "")
            bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if not bearer or not _hmac.compare_digest(bearer, token):
                return Response("Unauthorized", status=HTTPStatus.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
            return f(*args, **kwargs)
        return decorated
    return decorator


class LumenDBCollector:
    """Custom Prometheus collector serving the background-refreshed snapshot.

    This used to query the database on every scrape, taking a pooled connection
    to do it — so it competed for the pool it was reporting on and could block up
    to ``pool_timeout`` during exactly the burst it exists to describe. The
    queries now run on a daemon thread (``lumen/services/metrics_snapshot.py``)
    and ``collect()`` is a pure in-memory read. Nothing here may touch the DB.
    """

    def collect(self):
        from lumen.services.metrics_snapshot import get_snapshot

        snapshot = get_snapshot()

        # Before the first successful refresh, emit no lumen_model_* series at
        # all rather than zeros: absent → present is an ordinary new series to
        # Prometheus, whereas present(1e6) → present(0) → present(1e6) is two
        # counter resets and a fabricated rate spike.
        if snapshot.primed:
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

            for (model_name, source), usage in snapshot.model_usage.items():
                labels = [model_name, source]
                reqs_m.add_metric(labels, float(usage.requests))
                inp_m.add_metric(labels, float(usage.input_tokens))
                out_m.add_metric(labels, float(usage.output_tokens))
                cost_m.add_metric(labels, float(usage.cost))

            yield reqs_m
            yield inp_m
            yield out_m
            yield cost_m

            health_m = GaugeMetricFamily(
                "lumen_model_endpoint_healthy",
                "1=healthy 0=unhealthy per model endpoint",
                labels=["model", "endpoint_url"],
            )
            for model_name, url, healthy in snapshot.endpoint_health:
                health_m.add_metric([model_name, url], 1.0 if healthy else 0.0)
            yield health_m

            # active = admin has not disabled the user (Entity.active=True, the default)
            # total  = all users ever registered
            users_m = GaugeMetricFamily(
                "lumen_users",
                "User counts: active (not disabled by admin) and total",
                labels=["status"],
            )
            users_m.add_metric(["active"], float(snapshot.users_active))
            users_m.add_metric(["total"], float(snapshot.users_total))
            yield users_m

        # Emitted from here rather than as a prometheus_client Gauge: under
        # PROMETHEUS_MULTIPROC_DIR every multiprocess_mode is wrong for an age
        # (livesum reports 4x the age at 4 workers, mostrecent reports whichever
        # worker wrote last). Computed on the process serving the scrape, it
        # describes the same snapshot whose sample values are in this response.
        # Emitted while unprimed too (measured from process start), because that
        # is precisely the signal an operator alerts on for that state.
        yield GaugeMetricFamily(
            "lumen_metrics_snapshot_age_seconds",
            "Age of the in-memory metrics snapshot this response was built from",
            value=time.monotonic() - snapshot.captured_at,
        )

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
            # Checkouts held longer than any legitimate call site holds one (every
            # streaming path releases its connection before the LLM call), so this
            # rising is a leak rather than load. See services/pool_tracker.py.
            pool_m.add_metric(["stranded"], float(stranded_count()))
            if limit:
                pool_m.add_metric(["limit"], float(limit))
                if checked_out >= 0.8 * limit:
                    logger.warning(
                        "DB pool near capacity: %d/%d connections checked out",
                        checked_out, limit,
                    )
                watchdog(checked_out, limit)
        except AttributeError:
            # Pools without queue semantics (SQLite StaticPool/NullPool) lack these.
            pass
        yield pool_m


@metrics_bp.route("/metrics")
@_metrics_auth_required("token")
def metrics():
    # DB metrics — always globally accurate, queries the shared database
    db_output = generate_latest(current_app.config["PROMETHEUS_REGISTRY"])

    # HTTP metrics — use multiprocess aggregation if PROMETHEUS_MULTIPROC_DIR is set
    # (all workers write to shared dir; any worker can serve the full aggregate),
    # otherwise fall back to the default per-process registry.
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        from prometheus_client.multiprocess import MultiProcessCollector

        # Imported lazily: importing the middleware constructs its metric objects,
        # and those must not be created before PROMETHEUS_MULTIPROC_DIR is set.
        from lumen.blueprints.metrics.middleware import reap_dead_workers
        # Before the collector is built, not after: a worker killed by SIGKILL
        # never ran mark_process_dead, so its live gauges would otherwise be
        # merged into this scrape's aggregates.
        reap_dead_workers()
        mp_registry = CollectorRegistry()
        MultiProcessCollector(mp_registry)
        http_output = generate_latest(mp_registry)
    else:
        from prometheus_client import REGISTRY
        http_output = generate_latest(REGISTRY)

    return Response(db_output + http_output, status=HTTPStatus.OK, mimetype=CONTENT_TYPE_LATEST)


def _format_deployment() -> str:
    """Static deployment facts, so one capture is self-contained.

    The per-process pool numbers only mean something next to the multiplier they
    were divided by (worker processes x replicas) and the server budget they were
    divided out of (Postgres ``max_connections``). ``max_connections`` is queried
    live on a throwaway unpooled connection, so it works even while this
    process's own pool is exhausted — which is exactly when this endpoint runs.
    """
    from lumen.services.db_pool import query_max_connections, resolve_wsgi_workers

    cfg = current_app.config
    topo = cfg.get("POOL_TOPOLOGY", {})
    opts = cfg.get("SQLALCHEMY_ENGINE_OPTIONS", {})
    uri = cfg.get("SQLALCHEMY_DATABASE_URI", "")
    workers = topo.get("workers", "?")
    replicas = topo.get("replicas", "?")
    wsgi_threads = resolve_wsgi_workers(opts)
    live_wsgi = sum(1 for t in threading.enumerate() if t.name.startswith("WSGI_"))
    if uri.startswith("sqlite"):
        max_conn = "n/a (sqlite)"
    else:
        try:
            max_conn = query_max_connections(uri)
        except Exception as exc:  # DB unreachable — report it rather than 500 the capture
            max_conn = f"unavailable ({type(exc).__name__})"
    lines = [
        f"host: {socket.gethostname()}  app version: {cfg.get('APP_VERSION', '?')}",
        f"worker processes: {workers} (WEB_CONCURRENCY={os.environ.get('WEB_CONCURRENCY', 'unset')})"
        f"  replicas: {replicas} (LUMEN_REPLICAS={os.environ.get('LUMEN_REPLICAS', 'unset')})",
        f"wsgi thread pool: {wsgi_threads} per process"
        f" (LUMEN_WSGI_WORKERS={os.environ.get('LUMEN_WSGI_WORKERS', 'unset')}), {live_wsgi} spawned",
        f"engine options: pool_size={opts.get('pool_size', 'default')}"
        f" max_overflow={opts.get('max_overflow', 'default')}"
        f" pool_timeout={opts.get('pool_timeout', 'default')}"
        f" pool_recycle={opts.get('pool_recycle', 'default')}",
        f"postgres max_connections: {max_conn}",
    ]
    if isinstance(max_conn, int) and "pool_size" in opts:
        budget = (int(opts["pool_size"]) + int(opts.get("max_overflow", 0))) * int(workers) * int(replicas)
        lines.append(
            f"pool budget: (pool_size+max_overflow) x workers x replicas = {budget} of {max_conn}"
        )
    return "\n".join(lines) + "\n"


def _format_pool_status() -> str:
    """The pool's own count of checked-out connections.

    Printed alongside the tracker so one capture answers whether a reported
    checkout is real: the tracker naming a holder while checked_out is 0 means
    the entry is stale bookkeeping, not a leaked connection.
    """
    pool = db.engine.pool
    try:
        return (
            f"{type(pool).__name__}: size={pool.size()} checked_in={pool.checkedin()} "
            f"checked_out={pool.checkedout()} overflow={pool.overflow()} "
            f"max_overflow={pool._max_overflow}\n"
        )
    except AttributeError:
        # Pools without queue semantics (SQLite StaticPool/NullPool) lack these.
        return f"{type(pool).__name__}: no queue semantics, nothing to report\n"


@metrics_bp.route("/metrics/debug")
@_metrics_auth_required("debug_token", "token")
def metrics_debug():
    """Outstanding DB pool checkouts and a stack dump of every live thread.

    Two things the gauges can't show: which call site is holding a connection it
    never returned, and whether a WSGI worker thread is wedged mid-request (the
    a2wsgi thread pool is small, so a few stuck threads stop the app serving).
    Authenticated with api.prometheus.debug_token when configured (this endpoint
    exposes more than the gauges do), else the same bearer token as /metrics.
    """
    body = (
        "=== deployment ===\n"
        f"{_format_deployment()}"
        "\n=== DB pool status ===\n"
        f"{_format_pool_status()}"
        # A session left in the registry by a failed remove() pins its connection for
        # the life of the process, so in steady state this should not exceed the number
        # of in-flight requests. Matching the stranded count means the sessions were
        # never removed.
        f"sessions still registered: {len(db.session.registry.registry)}\n"
        # Answers, per app-context scope key, whether teardown ever ran for the
        # context a leaked checkout was created under — "teardown=NEVER" on a
        # still-registered key means the context was abandoned without pop().
        f"\n=== scope keys: checkouts vs teardowns vs session registry ===\n"
        f"{format_scope_report(list(db.session.registry.registry))}"
        # The push/pop probes' catches, with the stack that did it: a ctx= here
        # matching a teardown=NEVER key above names the moment it was orphaned.
        "\n=== app-context anomalies (double push / skipped teardown / leftover context) ===\n"
        f"{format_context_anomalies()}"
        f"\n=== DB pool checkouts held over {STRANDED_AFTER:.0f}s ===\n"
        f"{format_outstanding(min_age=STRANDED_AFTER)}"
        f"\n=== what retains those checkouts ===\n"
        f"{format_holders()}"
        "\n=== all DB pool checkouts ===\n"
        f"{format_outstanding()}"
        "\n=== thread dump ===\n"
        f"{thread_dump()}"
    )
    return Response(body, status=HTTPStatus.OK, mimetype="text/plain")
