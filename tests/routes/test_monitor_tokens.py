"""Per-model token counts on ``/v1/models?tokens=true``, for the monitoring token only (SQLite).

The clock is pinned with ``request_rates._now`` as in ``test_request_rates.py``:
NOW is 15 seconds into a minute, so a row 30 seconds old is in the previous
whole minute, which is what ``last_minute`` counts.
"""
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest

from lumen.services import request_rates

NOW = datetime(2026, 10, 20, 12, 0, 15, tzinfo=timezone.utc)
MONITOR = "monitor-tokens-token"
USER_KEY = "lk_test_tokens_key"
TOKEN_FIELDS = ("input_tokens", "output_tokens")
CACHE_FIELD = "cache_metrics"
#: What Lumen reports for a model whose backend sends no cache counts at all: the three
#: seeded rows inside the last day, none of them carrying a cache count.
SILENT = {"requests": 3, "reads": False, "writes": False}


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(request_rates, "_now", lambda: NOW)


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


def _log(model_id, ago, inputs, outputs, cached=None, write=None):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    db.session.add(RequestLog(time=NOW - ago, model_config_id=model_id, source="api",
                              input_tokens=inputs, output_tokens=outputs,
                              cached_input_tokens=cached, cache_write_tokens=write))


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
    }


@pytest.fixture
def cache_seeded(app, test_model):
    """test-model rows that report both cache counts, reads only, and neither."""
    with app.app_context():
        from lumen.extensions import db
        a = test_model["id"]
        _log(a, timedelta(minutes=5), 10, 1, cached=4, write=2)
        _log(a, timedelta(minutes=10), 10, 1, cached=3)
        _log(a, timedelta(minutes=15), 10, 1)
        db.session.commit()
    return {"requests": 3, "reads": True, "writes": True}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _listed(client, token, query=""):
    resp = client.get(f"/v1/models{query}", headers=_auth(token))
    assert resp.status_code == HTTPStatus.OK
    return {m["id"]: m for m in resp.get_json()["data"]}


def test_token_windows_reader(app, seeded, test_model):
    with app.app_context():
        assert request_rates.token_windows() == {
            test_model["id"]: seeded,
        }


def test_last_month_starts_thirty_days_before_the_current_minute(app, test_model):
    """last_month starts exactly 30 days before the current minute, inclusive.

    Windows are whole minute buckets, so the edge is ``minute - 30d``
    (2026-09-20 12:00:00), not ``NOW - 30d`` (12:00:15): a row at the edge is
    inside (``>=``) and a row one second earlier is outside.
    """
    minute = NOW.replace(second=0)
    with app.app_context():
        from lumen.extensions import db
        _log(test_model["id"], NOW - minute + timedelta(days=30), 1, 10)                     # 2026-09-20 12:00:00
        _log(test_model["id"], NOW - minute + timedelta(days=30, seconds=1), 100, 1000)      # 2026-09-20 11:59:59
        db.session.commit()
        assert request_rates.token_windows(test_model["id"]) == {
            test_model["id"]: {
                "input_tokens": {"last_minute": 0, "last_hour": 0, "last_day": 0, "last_month": 1},
                "output_tokens": {"last_minute": 0, "last_hour": 0, "last_day": 0, "last_month": 10},
            },
        }


def test_cache_reporting_reader(app, cache_seeded, test_model):
    with app.app_context():
        assert request_rates.cache_reporting() == {
            test_model["id"]: cache_seeded,
        }


def test_cache_reporting_separates_absent_counts_from_reported_zeros(app, test_model):
    """A backend that does its own cache accounting reports zeros; one without
    it sends nothing. Only the second is why a configured price would not bill."""
    with app.app_context():
        from lumen.extensions import db
        _log(test_model["id"], timedelta(minutes=5), 10, 1, cached=0, write=0)
        db.session.commit()
        assert request_rates.cache_reporting() == {
            test_model["id"]: {"requests": 1, "reads": True, "writes": True},
        }


def test_cache_reporting_reads_the_last_day_only(app, test_model):
    with app.app_context():
        from lumen.extensions import db
        _log(test_model["id"], timedelta(days=2), 10, 1, cached=4, write=2)
        db.session.commit()
        assert request_rates.cache_reporting() == {}


def test_monitor_list_models_with_cache_metrics(client, monitor_token, cache_seeded, test_model):
    row = _listed(client, monitor_token, "?tokens=true")[test_model["model_name"]]
    assert row[CACHE_FIELD] == cache_seeded


def test_monitor_get_model_with_cache_metrics(client, monitor_token, cache_seeded, test_model):
    resp = client.get(f"/v1/models/{test_model['model_name']}?tokens=true", headers=_auth(monitor_token))
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()[CACHE_FIELD] == cache_seeded


def test_monitor_reports_a_silent_backend_as_unreported(client, monitor_token, seeded, test_model):
    """Every seeded row predates the cache columns being written, so the
    backend reported nothing: the monitor needs that to tell a missing price
    from a backend that has none to report."""
    row = _listed(client, monitor_token, "?tokens=true")[test_model["model_name"]]
    assert row[CACHE_FIELD] == SILENT


def test_monitor_list_models_with_tokens(client, monitor_token, seeded, test_model):
    row = _listed(client, monitor_token, "?tokens=true")[test_model["model_name"]]
    for field in TOKEN_FIELDS:
        assert row[field] == seeded[field]
    # No combined field: the sum is the client's to take.
    assert "tokens" not in row


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
    # No traffic means no verdict on the cache, which the monitor reads as
    # "nothing to warn about" rather than "reports nothing".
    assert row[CACHE_FIELD] == {"requests": 0, "reads": False, "writes": False}


@pytest.mark.parametrize("query", ["", "?tokens=false", "?tokens="])
def test_monitor_without_the_parameter_gets_no_token_fields(client, monitor_token, seeded, test_model, query):
    row = _listed(client, monitor_token, query)[test_model["model_name"]]
    assert not set(TOKEN_FIELDS + (CACHE_FIELD,)) & row.keys()
    resp = client.get(f"/v1/models/{test_model['model_name']}{query}", headers=_auth(monitor_token))
    assert not set(TOKEN_FIELDS + (CACHE_FIELD,)) & resp.get_json().keys()


def test_user_key_with_tokens_matches_the_plain_response(client, user_key, seeded, test_model):
    assert _listed(client, user_key, "?tokens=true") == _listed(client, user_key)
    plain = client.get(f"/v1/models/{test_model['model_name']}", headers=_auth(user_key))
    with_tokens = client.get(f"/v1/models/{test_model['model_name']}?tokens=true", headers=_auth(user_key))
    assert with_tokens.status_code == HTTPStatus.OK
    assert with_tokens.get_json() == plain.get_json()
    assert not set(TOKEN_FIELDS + (CACHE_FIELD,)) & with_tokens.get_json().keys()


@pytest.mark.parametrize("method,path", [
    ("get", "/v1/usage?tokens=true"),
    ("post", "/v1/chat/completions?tokens=true"),
    ("post", "/v1/models/test-model/acknowledge?tokens=true"),
])
def test_monitor_token_still_cannot_reach_other_routes(client, monitor_token, test_model, method, path):
    resp = getattr(client, method)(path, headers=_auth(monitor_token), json={})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["type"] == "authentication_error"
