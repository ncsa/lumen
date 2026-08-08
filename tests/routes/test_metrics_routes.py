"""Tests for the /metrics Prometheus endpoint."""
from http import HTTPStatus


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
