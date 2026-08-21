"""How a rate-limited request answers the client.

Split out of test_api_auth.py so the rate-limit response contract has one
obvious home: two different conditions return 429 in this app (the limiter, and
an exhausted coin budget) and they need opposite reactions from the caller.
"""
from http import HTTPStatus

from tests.routes.test_api_auth import (
    _allow_model,
    _capturing_openai,
    _chat_post,
    _NonStreamResponse,
    api_key,
    fresh_rate_limit,
)

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "fresh_rate_limit"]

# ---------------------------------------------------------------------------
# Rate-limit response shape
# ---------------------------------------------------------------------------

def test_rate_limited_response_carries_retry_after(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """A bare 429 lets 300 clients retry on 300 independent schedules.

    The OpenAI SDK honours Retry-After, so supplying it is what stops a
    class-start burst from re-synchronising into a retry storm.
    """
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _capturing_openai(monkeypatch, routes, lambda **kw: _NonStreamResponse())

    # Spend the per-key budget, then one more.
    last = None
    for _ in range(40):
        last = _chat_post(client, token, test_model["model_name"], False)
        if last.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            break

    assert last.status_code == HTTPStatus.TOO_MANY_REQUESTS, "expected to hit the limiter"
    retry_after = last.headers.get("Retry-After")
    assert retry_after is not None, "429 must tell the client when to come back"
    assert retry_after.isdigit() and int(retry_after) >= 1
    # Derived from the limit's own window rather than hardcoded.
    assert int(retry_after) <= 3600

    body = last.get_json()
    assert body["error"]["type"] == "rate_limit_error"
    assert body["error"]["code"] == "rate_limit_exceeded"


def test_rate_limit_rejection_is_counted(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """The rejection taxonomy has to distinguish 'slow down' from 'out of coins'.

    Both are 429s today and were indistinguishable in metrics; the model label
    is legitimately empty here because the request body is never parsed.
    """
    from lumen.blueprints.api import routes
    recorded = []
    monkeypatch.setattr(
        "lumen.blueprints.metrics.middleware.observe_rejection",
        lambda reason, source, model="": recorded.append((reason, source, model)),
    )
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    _capturing_openai(monkeypatch, routes, lambda **kw: _NonStreamResponse())

    for _ in range(40):
        if _chat_post(client, token, test_model["model_name"], False).status_code == HTTPStatus.TOO_MANY_REQUESTS:
            break

    assert ("rate_limit", "api", "") in recorded


# ---------------------------------------------------------------------------
# The chat surface has the same two-kinds-of-429 problem as /v1
# ---------------------------------------------------------------------------

def _exhaust_chat_budget(app, entity_id, refresh_coins=10):
    from datetime import timedelta

    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.timeutils import utcnow
    with app.app_context():
        db.session.add(EntityLimit(
            entity_id=entity_id, max_coins=100,
            refresh_coins=refresh_coins, starting_coins=100,
        ))
        db.session.add(EntityBalance(
            entity_id=entity_id, coins_left=0,
            last_refill_at=utcnow() - timedelta(minutes=30),
        ))
        db.session.commit()


def test_chat_coin_exhaustion_sends_retry_after(app, auth_client, test_user, test_model):
    """Both 429s reach the chat surface, and only one used to say when to return.

    The limiter's 429 already carries Retry-After, so an exhausted budget without
    one is indistinguishable from a rate limit that clears in a moment — and the
    browser is not the only client of this endpoint.
    """
    _exhaust_chat_budget(app, test_user["id"])

    resp = auth_client.post("/chat/stream", json={
        "model": test_model["model_name"],
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert "Retry-After" in resp.headers
    assert 1 <= int(resp.headers["Retry-After"]) <= 60 * 60


def test_chat_coin_exhaustion_keeps_the_flat_error_body(app, auth_client, test_user, test_model):
    """chat.html renders `data.error` directly (chat.html:710).

    Nesting the body the way /v1 does would put "[object Object]" in the user's
    chat window, so the header is the only thing that changes here.
    """
    _exhaust_chat_budget(app, test_user["id"])

    resp = auth_client.post("/chat/stream", json={
        "model": test_model["model_name"],
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert isinstance(resp.get_json()["error"], str)


def test_chat_coin_exhaustion_sends_no_header_when_nothing_refills(
    app, auth_client, test_user, test_model
):
    """A pool with refresh_coins=0 never refills; a header would be a fabrication."""
    _exhaust_chat_budget(app, test_user["id"], refresh_coins=0)

    resp = auth_client.post("/chat/stream", json={
        "model": test_model["model_name"],
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert "Retry-After" not in resp.headers
