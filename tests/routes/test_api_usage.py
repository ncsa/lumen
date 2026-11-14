"""API key usage is visible only to the key that recorded it."""

from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from types import SimpleNamespace

from sqlalchemy import func, select

from lumen.blueprints.api import routes
from lumen.extensions import db, limiter
from lumen.models.api_key import APIKey
from lumen.models.entity_balance import EntityBalance
from lumen.models.request_log import RequestLog
from lumen.services.crypto import hash_api_key
from tests.routes.test_api_auth import _capturing_openai, _grant_unlimited_pool, _NonStreamResponse


def test_usage_returns_only_authenticated_keys_cumulative_counters(app, client, test_user):
    with app.app_context():
        first = APIKey(
            entity_id=test_user["id"], name="first", key_hash=hash_api_key("first-token"),
            requests=3, input_tokens=100, output_tokens=25, audio_seconds=42,
            cached_input_tokens=40, cache_write_tokens=10,
            cost=Decimal("1.234567"), last_used_at=datetime(2026, 9, 28, 14, 0),
        )
        second = APIKey(
            entity_id=test_user["id"], name="second", key_hash=hash_api_key("second-token"),
            requests=8, input_tokens=800, output_tokens=200, cost=Decimal("9.000000"),
        )
        db.session.add_all([first, second])
        db.session.commit()

    response = client.get("/v1/usage", headers={"Authorization": "Bearer first-token"})

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "requests": 3,
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "cache_write_tokens": 10,
        "output_tokens": 25,
        "total_tokens": 125,
        "audio_seconds": 42,
        "cost": 1.234567,
        "coins_available": None,
        "last_used_at": "2026-09-28T14:00:00Z",
    }
    assert client.get("/v1/usage", headers={"Authorization": "Bearer first-token"}).get_json() == response.get_json()
    assert client.get("/v1/usage", headers={"Authorization": "Bearer second-token"}).get_json()["requests"] == 8


def test_usage_requires_active_api_key(app, client, test_user):
    with app.app_context():
        db.session.add(APIKey(
            entity_id=test_user["id"], name="inactive", key_hash=hash_api_key("inactive-token"),
            revoked_at=datetime(2026, 1, 1),
        ))
        db.session.commit()

    assert client.get("/v1/usage").status_code == HTTPStatus.BAD_REQUEST
    assert client.get("/v1/usage", headers={"Authorization": "Bearer invalid"}).status_code == HTTPStatus.UNAUTHORIZED
    assert client.get("/v1/usage", headers={"Authorization": "Bearer inactive-token"}).status_code == HTTPStatus.UNAUTHORIZED


def test_usage_for_unused_key_has_zero_counters_and_no_last_use(app, client, test_user):
    with app.app_context():
        db.session.add(APIKey(
            entity_id=test_user["id"], name="unused", key_hash=hash_api_key("unused-token"),
        ))
        db.session.commit()

    response = client.get("/v1/usage", headers={"Authorization": "Bearer unused-token"})

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "audio_seconds": 0,
        "cost": 0.0,
        "coins_available": None,
        "last_used_at": None,
    }


def test_usage_reports_entity_coin_balance(app, client, test_user, restore_config):
    """coins_available mirrors the owning entity's pool: balance row, starting
    coins before first use, -2 for unlimited, null for no pool."""

    def balance_for(token):
        return client.get("/v1/usage", headers={"Authorization": f"Bearer {token}"}).get_json()["coins_available"]

    app.config["TOKEN_DEFAULTS"] = {"max": 100, "refresh": 10, "starting": 40}
    with app.app_context():
        db.session.add(APIKey(
            entity_id=test_user["id"], name="pooled", key_hash=hash_api_key("pooled-token"),
        ))
        db.session.commit()

    assert balance_for("pooled-token") == 40.0  # starting coins before the first request

    with app.app_context():
        db.session.add(EntityBalance(entity_id=test_user["id"], coins_left=Decimal("17.500000")))
        db.session.commit()

    assert balance_for("pooled-token") == 17.5

    app.config["TOKEN_DEFAULTS"] = {"max": -2, "refresh": 0, "starting": 0}
    assert balance_for("pooled-token") == -2


def test_usage_rejects_monitor_token(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "YAML_DATA", {
        **app.config.get("YAML_DATA", {}),
        "api": {"monitoring": {"token": "monitor-token"}},
    })

    response = client.get("/v1/usage", headers={"Authorization": "Bearer monitor-token"})

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_usage_cache_totals_match_the_keys_request_logs(app, client, monkeypatch, test_user, test_model,
                                                       test_model_endpoint):
    """Cache counts reported by the upstream add up on the calling key only; a
    request with no reported counts (NULL in request_logs) adds nothing."""
    with app.app_context():
        limiter.reset()
        _grant_unlimited_pool(app, test_user["id"])
        cached = APIKey(entity_id=test_user["id"], name="cached", key_hash=hash_api_key("cached-token"))
        other = APIKey(entity_id=test_user["id"], name="other", key_hash=hash_api_key("other-token"))
        db.session.add_all([cached, other])
        db.session.commit()
        cached_id = cached.id

    usages = iter([
        SimpleNamespace(prompt_tokens=1000, completion_tokens=10,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=600, cache_creation_input_tokens=300)),
        SimpleNamespace(prompt_tokens=500, completion_tokens=5,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=450)),
        SimpleNamespace(prompt_tokens=200, completion_tokens=2),  # no cache counts reported
        SimpleNamespace(prompt_tokens=9000, completion_tokens=90,  # the other key's traffic
                        prompt_tokens_details=SimpleNamespace(cached_tokens=7000, cache_creation_input_tokens=1000)),
    ])

    def _create(**kw):
        response = _NonStreamResponse()
        response.usage = next(usages)
        return response

    _capturing_openai(monkeypatch, routes, _create)
    for token in ("cached-token", "cached-token", "cached-token", "other-token"):
        result = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": test_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
        assert result.status_code == HTTPStatus.OK

    with app.app_context():
        logged = db.session.execute(
            select(func.sum(RequestLog.cached_input_tokens), func.sum(RequestLog.cache_write_tokens),
                   func.count(), func.count(RequestLog.cached_input_tokens))
            .where(RequestLog.api_key_id == cached_id)
        ).one()
        limiter.reset()
    assert tuple(logged) == (1050, 300, 3, 2)  # the third request logged NULL

    usage = client.get("/v1/usage", headers={"Authorization": "Bearer cached-token"}).get_json()
    assert (usage["cached_input_tokens"], usage["cache_write_tokens"]) == (logged[0], logged[1])
    assert (usage["requests"], usage["input_tokens"], usage["total_tokens"]) == (3, 1700, 1717)

    other_usage = client.get("/v1/usage", headers={"Authorization": "Bearer other-token"}).get_json()
    assert (other_usage["cached_input_tokens"], other_usage["cache_write_tokens"]) == (7000, 1000)
