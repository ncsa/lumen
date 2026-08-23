"""Tests for the /metrics Prometheus endpoint."""
import threading
import time
from http import HTTPStatus

from lumen.extensions import db


def _set_prometheus(app, config):
    original = app.config.get("YAML_DATA", {})
    app.config["YAML_DATA"] = {**original, "api": {**original.get("api", {}), "prometheus": config}}
    return original


def test_metrics_disabled_returns_404(client):
    resp = client.get("/metrics")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_metrics_enabled_no_token_returns_401(app, client):
    # At startup, missing token disables prometheus entirely (404).
    # This test bypasses startup by injecting YAML_DATA directly, so the
    # decorator still enforces 401 as a belt-and-suspenders check.
    original = _set_prometheus(app, {"enabled": True})
    try:
        resp = client.get("/metrics")
        assert resp.status_code == HTTPStatus.UNAUTHORIZED
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_enabled_with_correct_token_returns_200(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        resp = client.get("/metrics", headers={"Authorization": "Bearer secret"})
        assert resp.status_code == HTTPStatus.OK
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_enabled_with_wrong_token_returns_401(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        resp = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == HTTPStatus.UNAUTHORIZED
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_enabled_missing_auth_returns_401(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        resp = client.get("/metrics")
        assert resp.status_code == HTTPStatus.UNAUTHORIZED
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_cumulative_totals_are_counters(app, client):
    # The cumulative model totals must keep their _total names but be typed as
    # counters (not gauges), so dashboards keep working after the type change.
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        # Primed first: an unprimed snapshot deliberately emits no model series
        # at all (see test_unprimed_snapshot_emits_no_model_series).
        with app.app_context():
            from lumen.services.metrics_snapshot import refresh_snapshot
            refresh_snapshot()
        body = client.get("/metrics", headers={"Authorization": "Bearer secret"}).get_data(as_text=True)
    finally:
        app.config["YAML_DATA"] = original

    for name in (
        "lumen_model_requests_total",
        "lumen_model_input_tokens_total",
        "lumen_model_output_tokens_total",
        "lumen_model_cost_coins_total",
    ):
        assert f"# TYPE {name} counter" in body


def test_metrics_debug_requires_token(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        assert client.get("/metrics/debug").status_code == HTTPStatus.UNAUTHORIZED
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_debug_uses_debug_token_when_configured(app, client):
    """With debug_token set, /metrics/debug rejects the scrape token and accepts debug_token;
    /metrics keeps using the scrape token."""
    original = _set_prometheus(app, {"enabled": True, "token": "secret", "debug_token": "stronger"})
    try:
        assert client.get("/metrics/debug",
                          headers={"Authorization": "Bearer secret"}).status_code == HTTPStatus.UNAUTHORIZED
        assert client.get("/metrics/debug",
                          headers={"Authorization": "Bearer stronger"}).status_code == HTTPStatus.OK
        assert client.get("/metrics",
                          headers={"Authorization": "Bearer secret"}).status_code == HTTPStatus.OK
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_debug_falls_back_to_scrape_token(app, client):
    """Without debug_token, /metrics/debug accepts the scrape token as before."""
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        assert client.get("/metrics/debug",
                          headers={"Authorization": "Bearer secret"}).status_code == HTTPStatus.OK
    finally:
        app.config["YAML_DATA"] = original


def test_metrics_debug_returns_checkouts_and_thread_dump(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        resp = client.get("/metrics/debug", headers={"Authorization": "Bearer secret"})
    finally:
        app.config["YAML_DATA"] = original
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert "=== all DB pool checkouts ===" in body
    assert "=== thread dump ===" in body
    # The scope report cross-references checkouts against teardowns and the
    # session registry, so a capture can tell whether a leaked checkout's app
    # context ever went through teardown.
    assert "=== scope keys: checkouts vs teardowns vs session registry ===" in body
    assert "teardown(s) recorded" in body
    # The push/pop probes' catches ship in the same capture.
    assert "=== app-context anomalies" in body


def test_metrics_debug_reports_pool_status(app, client):
    """The pool's own view ships with the tracker's, so one capture can tell a
    real leaked connection from a stale tracker entry."""
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        resp = client.get("/metrics/debug", headers={"Authorization": "Bearer secret"})
    finally:
        app.config["YAML_DATA"] = original
    body = resp.get_data(as_text=True)
    assert "=== DB pool status ===" in body
    assert "=== what retains those checkouts ===" in body
    # The test app runs on SQLite, whose pool has no queue semantics.
    assert "checked_out=" in body or "no queue semantics" in body


def test_metrics_debug_reports_deployment_facts(app, client):
    """The capture carries the static sizing context — workers x replicas, the
    wsgi thread pool, engine options and the server's max_connections — so the
    live pool numbers can be judged without hunting through configs."""
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        resp = client.get("/metrics/debug", headers={"Authorization": "Bearer secret"})
    finally:
        app.config["YAML_DATA"] = original
    body = resp.get_data(as_text=True)
    assert "=== deployment ===" in body
    assert "worker processes:" in body
    assert "wsgi thread pool:" in body
    # The test app runs on SQLite, which has no max_connections.
    assert "postgres max_connections: n/a (sqlite)" in body


def test_metrics_exposes_stranded_pool_gauge(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        body = client.get("/metrics", headers={"Authorization": "Bearer secret"}).get_data(as_text=True)
    finally:
        app.config["YAML_DATA"] = original
    assert 'lumen_db_pool_connections{state="stranded"}' in body


def _count_statements_on_this_thread(engine):
    """Count SQL statements issued by the calling thread.

    Thread-scoped on purpose: the snapshot refresher is a daemon thread that
    queries on its own cadence, and its statements are exactly the ones this
    phase moved *off* the scrape path.
    """
    from sqlalchemy import event

    counted = []
    caller = threading.get_ident()

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if threading.get_ident() == caller:
            counted.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    return counted, lambda: event.remove(engine, "before_cursor_execute", before_cursor_execute)


def test_scraping_metrics_executes_no_sql(app, client):
    """The point of the phase: /metrics must not touch the database.

    It used to run a GROUP BY over model_stats plus two COUNT(*) over entities on
    every scrape, taking a pooled connection to do it — so it competed for the
    pool it was reporting on, and could block up to pool_timeout during exactly
    the burst it exists to describe.
    """
    with app.app_context():
        from lumen.services.metrics_snapshot import refresh_snapshot
        refresh_snapshot()
        engine = db.engine

    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    statements, unsubscribe = _count_statements_on_this_thread(engine)
    try:
        for _ in range(20):
            resp = client.get("/metrics", headers={"Authorization": "Bearer secret"})
            assert resp.status_code == HTTPStatus.OK
    finally:
        unsubscribe()
        app.config["YAML_DATA"] = original

    assert statements == []


def test_snapshot_age_is_exported_and_grows(app, client):
    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    try:
        with app.app_context():
            from lumen.services.metrics_snapshot import refresh_snapshot
            refresh_snapshot()
        first = _snapshot_age(client)
        time.sleep(0.05)
        second = _snapshot_age(client)
        with app.app_context():
            refresh_snapshot()
        after_refresh = _snapshot_age(client)
    finally:
        app.config["YAML_DATA"] = original

    # Staleness has to be visible rather than silent: the age grows while the
    # refresher is idle, and drops back when a pass lands.
    assert second > first
    assert after_refresh < second


def _snapshot_age(client) -> float:
    body = client.get("/metrics", headers={"Authorization": "Bearer secret"}).get_data(as_text=True)
    for line in body.splitlines():
        if line.startswith("lumen_metrics_snapshot_age_seconds "):
            return float(line.split()[1])
    raise AssertionError(f"lumen_metrics_snapshot_age_seconds missing from:\n{body}")


def test_unprimed_snapshot_emits_no_model_series(app, client):
    """Cold start: absent, not zero.

    absent -> present is an ordinary new series to Prometheus, whereas
    present(1e6) -> present(0) -> present(1e6) is two counter resets and a
    fabricated rate spike on every restart.
    """
    from lumen.services import metrics_snapshot

    original = _set_prometheus(app, {"enabled": True, "token": "secret"})
    saved = metrics_snapshot._snapshot
    try:
        metrics_snapshot._snapshot = metrics_snapshot._UNPRIMED
        body = client.get("/metrics", headers={"Authorization": "Bearer secret"}).get_data(as_text=True)
    finally:
        metrics_snapshot._snapshot = saved
        app.config["YAML_DATA"] = original

    assert "lumen_model_" not in body
    assert "lumen_users" not in body
    # The age gauge is still emitted while unprimed — it is precisely the signal
    # an operator alerts on for this state — as are the pool gauges.
    assert "lumen_metrics_snapshot_age_seconds " in body
    assert "lumen_db_pool_connections" in body


def test_snapshot_age_comes_from_the_collector_not_a_prometheus_gauge(app):
    """Contract (g): the age is yielded by LumenDBCollector, not exported as a
    prometheus_client Gauge.

    Under PROMETHEUS_MULTIPROC_DIR a Gauge must declare a multiprocess_mode and
    every mode is wrong for an age: livesum reports 4x the age at 4 workers,
    mostrecent reports whichever worker wrote last. Computed on the process
    serving the scrape, the age describes the same snapshot whose sample values
    are in the same response.
    """
    from prometheus_client import REGISTRY, generate_latest

    with app.app_context():
        collector_output = generate_latest(app.config["PROMETHEUS_REGISTRY"]).decode()
    default_output = generate_latest(REGISTRY).decode()

    assert "lumen_metrics_snapshot_age_seconds " in collector_output
    assert "lumen_metrics_snapshot_age_seconds" not in default_output
