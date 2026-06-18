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
