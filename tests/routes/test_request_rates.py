"""Per-model request rates on SQLite: the reader, /v1/models, and the model detail page.

The clock is pinned with ``request_rates._now`` so every row sits a known
distance from a minute edge. NOW is 15 seconds into a minute: a row 30 seconds
old is in the previous whole minute, which is exactly what ``last_minute``
counts.
"""
import re
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest

from lumen.blueprints.api import routes as api_routes
from lumen.services import request_rates

NOW = datetime(2026, 10, 20, 12, 0, 15, tzinfo=timezone.utc)
MONITOR = "monitor-rates-token"


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(request_rates, "_now", lambda: NOW)
    # /v1/models caches rates for 30s per worker; a cache another test filled
    # would hide the rows seeded here.
    monkeypatch.setitem(api_routes._rates_cache, "data", {})
    monkeypatch.setitem(api_routes._rates_cache, "expires_at", 0.0)


@pytest.fixture
def monitor_token(app):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                               "api": {"monitoring": {"token": MONITOR}}}
    yield MONITOR
    app.config["YAML_DATA"].pop("api", None)


def _log(model_id, ago, source="api", input_tokens=0, output_tokens=0):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    db.session.add(RequestLog(time=NOW - ago, model_config_id=model_id, source=source,
                              input_tokens=input_tokens, output_tokens=output_tokens))


@pytest.fixture
def seeded(app, test_model, test_model_endpoint):
    """Two models with rows at 30s, 30m, 5h and 2d ago, plus one unattributed row."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        other = ModelConfig(model_name="other-model", input_cost_per_million=1.0, output_cost_per_million=2.0)
        db.session.add(other)
        db.session.flush()
        a, b = test_model["id"], other.id

        for ago in (timedelta(seconds=30), timedelta(minutes=30), timedelta(hours=5), timedelta(days=2)):
            _log(a, ago)
        _log(b, timedelta(minutes=30), source="chat")
        _log(b, timedelta(minutes=30))
        _log(b, timedelta(hours=5))
        _log(b, timedelta(days=2))
        _log(None, timedelta(seconds=30))
        db.session.commit()
    return {
        "a": a, "b": b,
        "expected_a": {"last_minute": 1, "last_hour": 2, "last_day": 3},
        "expected_b": {"last_minute": 0, "last_hour": 2, "last_day": 3},
    }


def _requests(result):
    """The request counts out of ``rates()``; token sums are covered in test_api_auth."""
    return {model_id: metrics["requests"] for model_id, metrics in result.items()}


def test_rates_all_models(app, seeded):
    with app.app_context():
        result = _requests(request_rates.rates())
    assert result == {seeded["a"]: seeded["expected_a"], seeded["b"]: seeded["expected_b"]}


def test_rates_one_model(app, seeded):
    with app.app_context():
        assert _requests(request_rates.rates(seeded["b"])) == {seeded["b"]: seeded["expected_b"]}


def test_last_minute_is_the_previous_whole_minute(app, test_model):
    """The previous whole minute counts; the current partial minute does not."""
    minute = NOW.replace(second=0)
    with app.app_context():
        from lumen.extensions import db
        _log(test_model["id"], NOW - minute)                                   # 12:00:00, current minute
        _log(test_model["id"], NOW - minute + timedelta(seconds=10))           # 11:59:50
        _log(test_model["id"], NOW - minute + timedelta(minutes=1))            # 11:59:00
        _log(test_model["id"], NOW - minute + timedelta(minutes=1, seconds=1))  # 11:58:59
        db.session.commit()
        result = request_rates.rates(test_model["id"])[test_model["id"]]["requests"]
    assert result == {"last_minute": 2, "last_hour": 4, "last_day": 4}


def test_rows_after_the_current_minute_are_not_counted(app, test_model):
    """A row stamped in the future (clock skew) is in no window."""
    with app.app_context():
        from lumen.extensions import db
        _log(test_model["id"], timedelta(seconds=-40))   # 12:00:55, current minute
        _log(test_model["id"], timedelta(seconds=-45))   # 12:01:00, next minute
        _log(test_model["id"], timedelta(minutes=-30))
        db.session.commit()
        result = request_rates.rates(test_model["id"])[test_model["id"]]["requests"]
    assert result == {"last_minute": 0, "last_hour": 1, "last_day": 1}


def test_token_sums_share_the_request_windows(app, test_model):
    """Token sums use the same whole-minute edges as the request count."""
    with app.app_context():
        from lumen.extensions import db
        _log(test_model["id"], timedelta(seconds=30), input_tokens=1, output_tokens=2)       # previous minute
        _log(test_model["id"], timedelta(seconds=5), input_tokens=10, output_tokens=20)      # current minute
        _log(test_model["id"], timedelta(minutes=30), input_tokens=100, output_tokens=200)
        _log(test_model["id"], timedelta(hours=5), input_tokens=1000, output_tokens=2000)
        _log(test_model["id"], timedelta(days=2), input_tokens=10000, output_tokens=20000)
        _log(test_model["id"], timedelta(seconds=-45), input_tokens=7, output_tokens=7)     # next minute
        db.session.commit()
        result = request_rates.rates(test_model["id"])[test_model["id"]]
    assert result == {
        "requests": {"last_minute": 1, "last_hour": 3, "last_day": 4},
        "input_tokens": {"last_minute": 1, "last_hour": 111, "last_day": 1111},
        "output_tokens": {"last_minute": 2, "last_hour": 222, "last_day": 2222},
    }
    assert all(type(v) is int for metric in result.values() for v in metric.values())


def test_v1_models_reports_rates(client, monitor_token, seeded, test_model):
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {monitor_token}"})
    assert resp.status_code == HTTPStatus.OK
    models = {m["id"]: m for m in resp.get_json()["data"]}
    a = models[test_model["model_name"]]
    assert a["requests"] == seeded["expected_a"]
    assert a["status"] == "ok"
    assert a["instances"] == {"configured": 1, "healthy": 1}
    b = models["other-model"]
    assert b["requests"] == seeded["expected_b"]
    assert b["status"] == "down"
    assert b["instances"] == {"configured": 0, "healthy": 0}


def test_v1_get_model_reports_rates(client, monitor_token, seeded, test_model):
    resp = client.get(f"/v1/models/{test_model['model_name']}",
                      headers={"Authorization": f"Bearer {monitor_token}"})
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    assert body["requests"] == seeded["expected_a"]
    assert body["status"] == "ok"
    assert body["instances"] == {"configured": 1, "healthy": 1}


def test_v1_models_zero_for_a_model_without_requests(client, monitor_token, test_model):
    resp = client.get(f"/v1/models/{test_model['model_name']}",
                      headers={"Authorization": f"Bearer {monitor_token}"})
    assert resp.get_json()["requests"] == {"last_minute": 0, "last_hour": 0, "last_day": 0}


def _detail_count(html, label):
    match = re.search(rf"{re.escape(label)}</th>\s*<td[^>]*>(\d+)</td>", html)
    assert match, f"{label!r} row not found"
    return int(match.group(1))


def test_model_detail_renders_hour_and_day_counts(auth_client, seeded, test_model):
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    html = resp.get_data(as_text=True)
    assert _detail_count(html, "Requests / hr") == 2
    assert _detail_count(html, "Requests / 24h") == 3
