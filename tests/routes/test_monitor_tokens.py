"""Per-model token counts on ``/v1/models?tokens=true``, for the monitoring token only (SQLite).

The clock is pinned with ``request_rates._now`` as in ``test_request_rates.py``:
NOW is 15 seconds into a minute, so a row 30 seconds old is in the previous
whole minute, which is what ``last_minute`` counts.
"""
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest

from lumen.blueprints.api import routes as api_routes
from lumen.services import request_rates

NOW = datetime(2026, 10, 20, 12, 0, 15, tzinfo=timezone.utc)
MONITOR = "monitor-tokens-token"
USER_KEY = "lk_test_tokens_key"
TOKEN_FIELDS = ("tokens", "input_tokens", "output_tokens")


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(request_rates, "_now", lambda: NOW)
    # Both readers are cached 30s per worker; a cache another test filled would
    # hide the rows seeded here.
    for cache in (api_routes._rates_cache, api_routes._tokens_cache):
        monkeypatch.setitem(cache, "data", {})
        monkeypatch.setitem(cache, "expires_at", 0.0)


@pytest.fixture
def monitor_token(app):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                               "api": {"monitoring": {"token": MONITOR}}}
    yield MONITOR
    app.config["YAML_DATA"].pop("api", None)


@pytest.fixture
def user_key(app, test_user):
    """An API key for test_user, with an unlimited pool so it sees test-model."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.crypto import hash_api_key
        db.session.add(APIKey(entity_id=test_user["id"], name="tokens-key", key_hash=hash_api_key(USER_KEY)))
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
    return USER_KEY


def _log(model_id, ago, inputs, outputs):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    db.session.add(RequestLog(time=NOW - ago, model_config_id=model_id, source="api",
                              input_tokens=inputs, output_tokens=outputs))


@pytest.fixture
def seeded(app, test_model, test_model_endpoint):
    """test-model has rows 30s, 30m, 5h, 2d and 40d ago; one row has no model."""
    with app.app_context():
        from lumen.extensions import db
        a = test_model["id"]
        _log(a, timedelta(seconds=30), 1, 10)
        _log(a, timedelta(minutes=30), 2, 20)
        _log(a, timedelta(hours=5), 4, 40)
        _log(a, timedelta(days=2), 8, 80)
        _log(a, timedelta(days=40), 16, 160)
        _log(None, timedelta(seconds=30), 1000, 1000)
        db.session.commit()
    return {
        "input_tokens": {"last_minute": 1, "last_hour": 3, "last_day": 7, "last_month": 15},
        "output_tokens": {"last_minute": 10, "last_hour": 30, "last_day": 70, "last_month": 150},
        "tokens": {"last_minute": 11, "last_hour": 33, "last_day": 77, "last_month": 165},
    }


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _listed(client, token, query=""):
    resp = client.get(f"/v1/models{query}", headers=_auth(token))
    assert resp.status_code == HTTPStatus.OK
    return {m["id"]: m for m in resp.get_json()["data"]}


def test_token_windows_reader(app, seeded, test_model):
    with app.app_context():
        assert request_rates.token_windows() == {
            test_model["id"]: {k: seeded[k] for k in ("input_tokens", "output_tokens")},
        }


def test_monitor_list_models_with_tokens(client, monitor_token, seeded, test_model):
    row = _listed(client, monitor_token, "?tokens=true")[test_model["model_name"]]
    for field in TOKEN_FIELDS:
        assert row[field] == seeded[field]


def test_monitor_get_model_with_tokens(client, monitor_token, seeded, test_model):
    resp = client.get(f"/v1/models/{test_model['model_name']}?tokens=true", headers=_auth(monitor_token))
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    for field in TOKEN_FIELDS:
        assert body[field] == seeded[field]


def test_monitor_model_without_requests_gets_zero_windows(client, monitor_token, test_model):
    row = _listed(client, monitor_token, "?tokens=true")[test_model["model_name"]]
    zero = {"last_minute": 0, "last_hour": 0, "last_day": 0, "last_month": 0}
    for field in TOKEN_FIELDS:
        assert row[field] == zero


@pytest.mark.parametrize("query", ["", "?tokens=false", "?tokens="])
def test_monitor_without_the_parameter_gets_no_token_fields(client, monitor_token, seeded, test_model, query):
    row = _listed(client, monitor_token, query)[test_model["model_name"]]
    assert not set(TOKEN_FIELDS) & row.keys()
    resp = client.get(f"/v1/models/{test_model['model_name']}{query}", headers=_auth(monitor_token))
    assert not set(TOKEN_FIELDS) & resp.get_json().keys()


def test_user_key_with_tokens_matches_the_plain_response(client, user_key, seeded, test_model):
    assert _listed(client, user_key, "?tokens=true") == _listed(client, user_key)
    plain = client.get(f"/v1/models/{test_model['model_name']}", headers=_auth(user_key))
    with_tokens = client.get(f"/v1/models/{test_model['model_name']}?tokens=true", headers=_auth(user_key))
    assert with_tokens.status_code == HTTPStatus.OK
    assert with_tokens.get_json() == plain.get_json()
    assert not set(TOKEN_FIELDS) & with_tokens.get_json().keys()


@pytest.mark.parametrize("method,path", [
    ("get", "/v1/usage?tokens=true"),
    ("post", "/v1/chat/completions?tokens=true"),
    ("post", "/v1/models/test-model/acknowledge?tokens=true"),
])
def test_monitor_token_still_cannot_reach_other_routes(client, monitor_token, test_model, method, path):
    resp = getattr(client, method)(path, headers=_auth(monitor_token), json={})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["type"] == "authentication_error"
