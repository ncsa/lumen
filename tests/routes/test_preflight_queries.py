"""Exact SQL statements a proxied request runs before its upstream call.

Same technique as ``test_list_models_reads_consent_rows_once``: a
``before_cursor_execute`` listener records every statement, and the faked
upstream call drops a marker into the same list, so "before the upstream call"
is just the prefix up to the marker. Each statement is reduced to the tables it
reads (in order of appearance), which is what the preflight budget is about and
does not churn when a column is added.

The model and its endpoint come from the per-process model snapshot, which
``_recording`` loads before it starts listening, as on a warm process: no
statement here reads ``model_configs`` or ``model_endpoints``.
"""
import re
from contextlib import contextmanager
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

import pytest
from sqlalchemy import event

from lumen.services import model_cache
from tests.conftest import make_group_with_member
from tests.routes.test_api_auth import (
    _capturing_openai,
    _chat_post,
    _set_model_flag,
    _UsageChunk,
    api_key,
    fresh_rate_limit,
)
from tests.routes.test_chat_routes import _Chunk, _fake_openai, _Usage

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "fresh_rate_limit"]

UPSTREAM = "UPSTREAM"
CHECKOUT = "CHECKOUT"
_TABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", re.IGNORECASE)

API_KEY = ("api_keys", "entities")
ENTITY = ("entities",)
CONSENT = ("entity_model_consents",)
POOL = ("entities", "entity_limits", "entity_balances", "group_members", "groups", "group_limits")


def _tables(statement):
    seen = []
    for t in _TABLE.findall(statement):
        if t not in seen:
            seen.append(t)
    return tuple(seen)


@contextmanager
def _recording(app, checkouts=False):
    """Yield the list every SQL statement (as its tables) and the UPSTREAM marker land in.

    With ``checkouts``, each pool connection checkout lands there too, as CHECKOUT.
    The model snapshot is loaded first, so it reflects every row written so far.
    """
    from lumen.extensions import db
    log = []
    model_cache.refresh(app)

    def _record(conn, cursor, statement, parameters, context, executemany):
        log.append(_tables(statement))

    def _checkout(dbapi_conn, record, proxy):
        log.append(CHECKOUT)

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _record)
    if checkouts:
        event.listen(engine.pool, "checkout", _checkout)
    try:
        yield log
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        if checkouts:
            event.remove(engine.pool, "checkout", _checkout)


def _before_upstream(log):
    assert UPSTREAM in log, "the request never reached the upstream call"
    return log[:log.index(UPSTREAM)]


def _own_limit(app, entity_id, max_coins=100, coins_left=50, refresh_coins=10):
    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.timeutils import utcnow
    with app.app_context():
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=max_coins,
                                   refresh_coins=refresh_coins, starting_coins=max_coins))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=coins_left,
                                     last_refill_at=utcnow() - timedelta(minutes=30)))
        db.session.commit()


def _group_pool(app, entity_id):
    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.group_limit import GroupLimit
    from lumen.timeutils import utcnow
    with app.app_context():
        group_id = make_group_with_member(entity_id)
        db.session.add(GroupLimit(group_id=group_id, max_coins=100, refresh_coins=10, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=50, last_refill_at=utcnow()))
        db.session.commit()


def _streaming_api_statements(app, client, monkeypatch, token, model_name):
    from lumen.blueprints.api import routes
    with _recording(app) as log:
        def _create(**kwargs):
            log.append(UPSTREAM)
            return iter([_UsageChunk()])
        _capturing_openai(monkeypatch, routes, _create)
        resp = _chat_post(client, token, model_name, True)
        assert resp.status_code == HTTPStatus.OK
        resp.get_data()
    return _before_upstream(log)


def test_streaming_api_preflight_group_pool_user(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """The pool comes from a group: still one statement for pool and balance."""
    token, _ = api_key
    _group_pool(app, test_user["id"])
    assert _streaming_api_statements(app, client, monkeypatch, token, test_model["model_name"]) == [
        API_KEY, POOL,
    ]


def test_streaming_api_preflight_own_limit_user(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    token, _ = api_key
    _own_limit(app, test_user["id"])
    assert _streaming_api_statements(app, client, monkeypatch, token, test_model["model_name"]) == [
        API_KEY, POOL,
    ]


def test_streaming_api_preflight_acknowledged_model_reads_consent_once(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """Only a model with an acknowledgement requirement reads the consent row."""
    from lumen.extensions import db
    from lumen.models.entity_model_consent import EntityModelConsent
    from lumen.timeutils import utcnow
    token, _ = api_key
    _own_limit(app, test_user["id"])
    with app.app_context():
        _set_model_flag(app, test_model, needs_ack=True)
        db.session.add(EntityModelConsent(entity_id=test_user["id"], model_config_id=test_model["id"],
                                          consented_at=utcnow()))
        db.session.commit()
    assert _streaming_api_statements(app, client, monkeypatch, token, test_model["model_name"]) == [
        API_KEY, CONSENT, POOL,
    ]


def test_chat_stream_preflight(app, auth_client, test_user, test_model, test_model_endpoint):
    """/chat/stream: the session's entity and one pool statement, in the view;
    the model and endpoint come from the snapshot.

    store_conversations comes from the entity login_required already loaded.
    The view hands the generator the endpoint it picked, so the generator reads
    no model or endpoint and checks out no connection before the upstream call:
    the whole prefix runs on the view's single checkout.
    """
    _group_pool(app, test_user["id"])
    fake = _fake_openai([_Chunk(content="hi"), _Chunk(usage=_Usage())])
    upstream = fake.return_value.chat.completions.create
    chunks = upstream.return_value
    with _recording(app, checkouts=True) as log:
        def _create(**kwargs):
            log.append(UPSTREAM)
            return chunks
        upstream.side_effect = _create
        with patch("lumen.services.llm.openai.OpenAI", fake):
            resp = auth_client.post("/chat/stream", json={
                "model": test_model["model_name"],
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == HTTPStatus.OK
            resp.get_data()
    assert _before_upstream(log) == [CHECKOUT, ENTITY, POOL]


# ---------------------------------------------------------------------------
# Refusals explain themselves from what the budget check already loaded
# ---------------------------------------------------------------------------

def test_api_403_runs_no_queries_after_the_budget_check(
    app, client, test_user, test_model, api_key, fresh_rate_limit,
):
    token, _ = api_key
    _own_limit(app, test_user["id"])
    with app.app_context():
        _set_model_flag(app, test_model, needs_ack=True)
    with _recording(app) as log:
        resp = _chat_post(client, token, test_model["model_name"], True)
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["code"] == "consent_required"
    assert log == [API_KEY, CONSENT, POOL]


def test_api_429_runs_no_queries_after_the_budget_check(
    app, client, test_user, test_model, api_key, fresh_rate_limit,
):
    token, _ = api_key
    _own_limit(app, test_user["id"], coins_left=0)
    with _recording(app) as log:
        resp = _chat_post(client, token, test_model["model_name"], True)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert 1 <= int(resp.headers["Retry-After"]) <= 60 * 60
    assert log == [API_KEY, POOL]


def test_chat_429_runs_no_queries_after_the_budget_check(app, auth_client, test_user, test_model):
    _own_limit(app, test_user["id"], coins_left=0)
    with _recording(app) as log:
        resp = auth_client.post("/chat/stream", json={
            "model": test_model["model_name"],
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert 1 <= int(resp.headers["Retry-After"]) <= 60 * 60
    assert log == [ENTITY, POOL]


# ---------------------------------------------------------------------------
# A snapshot that cannot be loaded is a 503, never an over-age fallback
# ---------------------------------------------------------------------------

def _break_snapshot(app, monkeypatch, failure):
    """Make the hot path's snapshot read fail the way ``failure`` names."""
    if failure == "endpoint":
        def _unavailable(model_config_id):
            raise model_cache.SnapshotUnavailable("model snapshot could not be loaded")
        monkeypatch.setattr(model_cache, "next_endpoint", _unavailable)
        return
    if failure == "over_age":
        model_cache.refresh(app)
        monkeypatch.setattr(model_cache, "MAX_AGE", -1)

    def _failing_load():
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(model_cache, "_load", _failing_load)


@pytest.mark.parametrize("failure", ["first_load", "over_age", "endpoint"])
def test_api_503_when_the_snapshot_cannot_be_loaded(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit, failure,
):
    token, _ = api_key
    _own_limit(app, test_user["id"])
    _break_snapshot(app, monkeypatch, failure)
    resp = _chat_post(client, token, test_model["model_name"], True)
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.get_json()["error"]["type"] == "server_error"


@pytest.mark.parametrize("failure", ["first_load", "over_age", "endpoint"])
def test_chat_503_when_the_snapshot_cannot_be_loaded(
    app, auth_client, monkeypatch, test_user, test_model, test_model_endpoint, failure,
):
    _own_limit(app, test_user["id"])
    _break_snapshot(app, monkeypatch, failure)
    resp = auth_client.post("/chat/stream", json={
        "model": test_model["model_name"],
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.get_json() == {"error": "Models are temporarily unavailable, try again shortly"}
