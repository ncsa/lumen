"""Tests for the /metrics Prometheus endpoint."""
from http import HTTPStatus


def _set_prometheus(app, config):
    original = app.config.get("YAML_DATA", {})
    app.config["YAML_DATA"] = {**original, "prometheus": config}
    return original


def test_metrics_disabled_returns_404(client):
    resp = client.get("/metrics")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_metrics_enabled_no_token_returns_200(app, client):
    original = _set_prometheus(app, {"enabled": True})
    try:
        resp = client.get("/metrics")
        assert resp.status_code == HTTPStatus.OK
        assert b"lumen_model_requests_total" in resp.data
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
