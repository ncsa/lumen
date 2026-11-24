"""``GET /v1/openapi.yaml`` serves the API spec publicly, pointed at this instance."""
from http import HTTPStatus

import pytest
import yaml
from sqlalchemy import event

from lumen.extensions import db


def test_openapi_spec_is_public_yaml_for_this_host(client):
    resp = client.get("/v1/openapi.yaml")

    assert resp.status_code == HTTPStatus.OK
    assert resp.mimetype == "application/yaml"
    spec = yaml.safe_load(resp.get_data(as_text=True))
    assert spec["openapi"].startswith("3.")
    assert spec["servers"][0]["url"] == "http://localhost"
    assert "https://lumen.example.com" not in resp.get_data(as_text=True)
    assert "/v1/openapi.yaml" in spec["paths"]


def test_openapi_spec_follows_the_request_host(client):
    resp = client.get("/v1/openapi.yaml", base_url="https://lumen.uni.edu")

    spec = yaml.safe_load(resp.get_data(as_text=True))
    assert spec["servers"][0]["url"] == "https://lumen.uni.edu"


@pytest.mark.parametrize("host", [
    "lumen.uni.edu:8443",
    "10.0.0.5",
    "[::1]:5000",
])
def test_openapi_spec_accepts_bare_origins(client, host):
    resp = client.get("/v1/openapi.yaml", headers={"Host": host})

    spec = yaml.safe_load(resp.get_data(as_text=True))
    assert spec["servers"][0]["url"] == f"http://{host}"


@pytest.mark.parametrize("headers", [
    {"Host": "evil.com/ x: {a: 1}"},
    {"Host": "evil.com#frag"},
    {"Host": 'evil.com"x: 1'},
    {"Host": "user@evil.com"},
    {"Host": "evil.com/path"},
    {"Host": "evil_host.com"},
    {"Host": "evil.com:99999999"},
    {"X-Forwarded-Host": "evil.com/ x: {a: 1}"},
])
def test_openapi_spec_rejects_hosts_that_are_not_bare_origins(client, headers):
    """A crafted Host or X-Forwarded-Host never reaches the YAML: the placeholder stays."""
    resp = client.get("/v1/openapi.yaml", headers=headers)

    assert resp.status_code == HTTPStatus.OK
    spec = yaml.safe_load(resp.get_data(as_text=True))
    assert spec["servers"] == [{"url": "https://lumen.example.com"}]
    assert "evil" not in resp.get_data(as_text=True)


def test_openapi_spec_forwarded_host_is_used_when_valid(client):
    resp = client.get("/v1/openapi.yaml", headers={"X-Forwarded-Host": "lumen.uni.edu", "X-Forwarded-Proto": "https"})

    spec = yaml.safe_load(resp.get_data(as_text=True))
    assert spec["servers"][0]["url"] == "https://lumen.uni.edu"


def test_openapi_spec_is_never_cached(client):
    """The body depends on the request's host, so no shared cache may replay it."""
    resp = client.get("/v1/openapi.yaml")

    assert resp.headers["Cache-Control"] == "no-store"
    assert "Host" in resp.headers["Vary"]


def test_openapi_spec_has_cors_headers(client):
    resp = client.get("/v1/openapi.yaml", headers={"Origin": "https://app.example.org"})

    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in resp.headers["Access-Control-Allow-Methods"]


def test_openapi_spec_ignores_a_bad_key_and_skips_the_db(app, client):
    """No key lookup: even a bogus Authorization header gets the spec, with no query."""
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        resp = client.get("/v1/openapi.yaml", headers={"Authorization": "Bearer not-a-key"})
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert resp.status_code == HTTPStatus.OK
    assert statements == []
