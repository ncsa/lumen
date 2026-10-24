"""Exact SQL statements a proxied request runs before its upstream call.

Same technique as ``test_list_models_reads_consent_rows_once``: a
``before_cursor_execute`` listener records every statement, and the faked
upstream call drops a marker into the same list, so "before the upstream call"
is just the prefix up to the marker. Each statement is reduced to the tables it
reads (in order of appearance), which is what the preflight budget is about and
does not churn when a column is added.
"""
import re
from contextlib import contextmanager
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

from sqlalchemy import event

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
_TABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", re.IGNORECASE)

API_KEY = ("api_keys",)
ENTITY = ("entities",)
MODEL = ("model_configs",)
CONSENT = ("entity_model_consents",)
POOL = ("entities", "entity_limits", "entity_balances", "group_members", "groups", "group_limits")
ENDPOINTS = ("model_endpoints",)


def _tables(statement):
    seen = []
    for t in _TABLE.findall(statement):
        if t not in seen:
            seen.append(t)
    return tuple(seen)


@contextmanager
def _recording(app):
    """Yield the list every SQL statement (as its tables) and the UPSTREAM marker land in."""
    from lumen.extensions import db
    log = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        log.append(_tables(statement))

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield log
    finally:
        event.remove(engine, "before_cursor_execute", _record)


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
        API_KEY, ENTITY, MODEL, POOL, ENDPOINTS,
    ]


def test_streaming_api_preflight_own_limit_user(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    token, _ = api_key
    _own_limit(app, test_user["id"])
    assert _streaming_api_statements(app, client, monkeypatch, token, test_model["model_name"]) == [
        API_KEY, ENTITY, MODEL, POOL, ENDPOINTS,
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
        API_KEY, ENTITY, MODEL, CONSENT, POOL, ENDPOINTS,
    ]


def test_chat_stream_preflight(app, auth_client, test_user, test_model, test_model_endpoint):
    """/chat/stream: the session's entity, the model, then one pool statement.

    The generator's own model and endpoint reads, and the store_conversations
    read, are later steps' business; they are listed so a change shows here.
    """
    _group_pool(app, test_user["id"])
    fake = _fake_openai([_Chunk(content="hi"), _Chunk(usage=_Usage())])
    upstream = fake.return_value.chat.completions.create
    chunks = upstream.return_value
    with _recording(app) as log:
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
    assert _before_upstream(log) == [ENTITY, MODEL, POOL, ENTITY, MODEL, ENDPOINTS]


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
    assert log == [API_KEY, ENTITY, MODEL, CONSENT, POOL]


def test_api_429_runs_no_queries_after_the_budget_check(
    app, client, test_user, test_model, api_key, fresh_rate_limit,
):
    token, _ = api_key
    _own_limit(app, test_user["id"], coins_left=0)
    with _recording(app) as log:
        resp = _chat_post(client, token, test_model["model_name"], True)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert 1 <= int(resp.headers["Retry-After"]) <= 60 * 60
    assert log == [API_KEY, ENTITY, MODEL, POOL]


def test_chat_429_runs_no_queries_after_the_budget_check(app, auth_client, test_user, test_model):
    _own_limit(app, test_user["id"], coins_left=0)
    with _recording(app) as log:
        resp = auth_client.post("/chat/stream", json={
            "model": test_model["model_name"],
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert 1 <= int(resp.headers["Retry-After"]) <= 60 * 60
    assert log == [ENTITY, MODEL, POOL]
