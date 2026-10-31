"""The order in which every billing path writes its tables.

Billing takes per-entity row locks (``entity_stats``, ``entity_balances``) that
every request of the entity queues on, so they must be taken last and released
by the very next statement, the COMMIT; and because every billing path takes
its locks in the same order, two billing transactions cannot deadlock. All of
that holds only while each path goes through ``bill_request``, which is what
these tests pin.

Same technique as ``test_preflight_queries``: a ``before_cursor_execute``
listener records every statement as the tables it touches, and the faked
upstream call drops an UPSTREAM marker into the same list. An engine ``commit``
listener drops a COMMIT marker, so the billing transaction is the span between
UPSTREAM and the first COMMIT after it. Only writes are kept, and a table
written twice in a row (an entity's first use: UPDATE, then INSERT) counts once.

Each path is pinned on both sides of the stats cutover: before it (the suite's
default, no ``stats_fold_state`` row) billing bumps ``model_stats`` and
``entity_stats``; after it (``post_cutover``) the fold does, and billing writes
only ``request_logs``, ``api_keys`` and ``entity_balances``, with the
``request_logs`` row marked ``stats_deferred``.
"""
from http import HTTPStatus
from unittest.mock import patch

import pytest
from sqlalchemy import event

from tests.routes.test_api_audio import _audio_data
from tests.routes.test_api_auth import (
    _capturing_openai,
    _chat_post,
    _NonStreamResponse,
    _UsageChunk,
    api_key,
    fresh_rate_limit,
)
from tests.routes.test_chat_routes import _Chunk, _fake_openai, _Usage
from tests.routes.test_preflight_queries import UPSTREAM, _own_limit, _recording, _tables

# Re-exported so pytest resolves the fixtures by name in this module's tests.
__all__ = ["api_key", "fresh_rate_limit"]

COMMIT = "COMMIT"
_WRITES = ("INSERT", "UPDATE", "DELETE")

BILLING = ["request_logs", "model_stats", "entity_stats", "entity_balances"]
API_BILLING = ["request_logs", "model_stats", "entity_stats", "api_keys", "entity_balances"]

# After the stats cutover: the fold, not billing, updates the stats tables.
CUTOVER_BILLING = ["request_logs", "entity_balances"]
CUTOVER_API_BILLING = ["request_logs", "api_keys", "entity_balances"]


@pytest.fixture(params=["before_cutover", "after_cutover"])
def cutover(request):
    """Run the test on both sides of the stats cutover; True after it."""
    if request.param == "after_cutover":
        request.getfixturevalue("post_cutover")
        return True
    return False


class _writes:
    """Record each write statement's table, plus UPSTREAM and COMMIT markers.

    Wraps ``_recording`` with a second ``before_cursor_execute`` listener that
    keeps the statement verb, so reads can be dropped, and a ``commit`` listener.
    """

    def __init__(self, app):
        self.app = app
        self.log = []

    def __enter__(self):
        from lumen.extensions import db
        self._recording = _recording(self.app)
        self._recording.__enter__()  # loads the model snapshot first
        with self.app.app_context():
            self._engine = db.engine
        event.listen(self._engine, "before_cursor_execute", self._record)
        event.listen(self._engine, "commit", self._commit)
        return self.log

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._record)
        event.remove(self._engine, "commit", self._commit)
        return self._recording.__exit__(*exc)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in _WRITES:
            self.log.append(_tables(statement)[0])

    def _commit(self, conn):
        self.log.append(COMMIT)


def _collapse(tables):
    out = []
    for t in tables:
        if not out or out[-1] != t:
            out.append(t)
    return out


def _billing(log):
    """The tables written between the upstream call and the first COMMIT after it."""
    assert UPSTREAM in log, "the request never reached the upstream call"
    after = log[log.index(UPSTREAM) + 1:]
    assert COMMIT in after, f"billing never committed: {log}"
    return _collapse(after[:after.index(COMMIT)])


def _marker(app):
    """``stats_deferred`` of the one request_logs row the request wrote.

    The same INSERT carries it: TRUE after the cutover (the fold adds the row),
    NULL before it (billing bumped the stats tables itself).
    """
    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    with app.app_context():
        return db.session.execute(select(RequestLog.stats_deferred)).scalar_one()


def _upstream(log, result):
    def _create(**kwargs):
        log.append(UPSTREAM)
        return result
    return _create


def test_api_non_stream_bills_hot_locks_last(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit, cutover,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    _own_limit(app, test_user["id"])
    with _writes(app) as log:
        _capturing_openai(monkeypatch, routes, _upstream(log, _NonStreamResponse()))
        resp = _chat_post(client, token, test_model["model_name"], False)
        assert resp.status_code == HTTPStatus.OK
    assert _billing(log) == (CUTOVER_API_BILLING if cutover else API_BILLING)
    assert _marker(app) is (True if cutover else None)


def test_api_stream_bills_hot_locks_last(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit, cutover,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    _own_limit(app, test_user["id"])
    with _writes(app) as log:
        _capturing_openai(monkeypatch, routes, _upstream(log, iter([_UsageChunk()])))
        resp = _chat_post(client, token, test_model["model_name"], True)
        assert resp.status_code == HTTPStatus.OK
        resp.get_data()
    assert _billing(log) == (CUTOVER_API_BILLING if cutover else API_BILLING)
    assert _marker(app) is (True if cutover else None)


def test_api_aborted_stream_bills_hot_locks_last(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit, cutover,
):
    """A client walking away mid-stream is billed through the same path, key included."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    _own_limit(app, test_user["id"])
    with _writes(app) as log:
        _capturing_openai(monkeypatch, routes, _upstream(log, iter([_UsageChunk(), _UsageChunk()])))
        resp = _chat_post(client, token, test_model["model_name"], True)
        assert resp.is_streamed
        next(iter(resp.response))  # one event, then walk away mid-stream
        resp.close()
    assert _billing(log) == (CUTOVER_API_BILLING if cutover else API_BILLING)
    assert _marker(app) is (True if cutover else None)

    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        assert db.session.execute(select(RequestLog.aborted)).scalar_one() is True


def test_audio_bills_hot_locks_last(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit, cutover,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    _own_limit(app, test_user["id"])

    class _Transcription:
        def model_dump(self):
            return {"text": "hello", "usage": {"type": "duration", "seconds": 3}}

    with _writes(app) as log:
        _capturing_openai(monkeypatch, routes, _upstream(log, _Transcription()))
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(model=test_model["model_name"]),
            content_type="multipart/form-data",
        )
        assert resp.status_code == HTTPStatus.OK
    assert _billing(log) == (CUTOVER_API_BILLING if cutover else API_BILLING)
    assert _marker(app) is (True if cutover else None)


def test_chat_stream_bills_hot_locks_last(app, auth_client, test_user, test_model, test_model_endpoint, cutover):
    """Billing commits before the conversation is written, in its own transaction.

    With storage on, that conversation transaction is not part of the billing
    order, but it takes the entity_stats lock (the new-conversation counter)
    as its last write, just before its own COMMIT. That counter stays
    synchronous after the stats cutover too.
    """
    _own_limit(app, test_user["id"])
    fake = _fake_openai([_Chunk(content="hi"), _Chunk(usage=_Usage())])
    upstream = fake.return_value.chat.completions.create
    with _writes(app) as log:
        upstream.side_effect = _upstream(log, upstream.return_value)
        with patch("lumen.services.llm.openai.OpenAI", fake):
            resp = auth_client.post("/chat/stream", json={
                "model": test_model["model_name"],
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.status_code == HTTPStatus.OK
            resp.get_data()
    assert _billing(log) == (CUTOVER_BILLING if cutover else BILLING)
    assert _marker(app) is (True if cutover else None)

    after = log[log.index(UPSTREAM) + 1:]
    conversation = after[after.index(COMMIT) + 1:]
    assert COMMIT in conversation, f"the conversation was never committed: {log}"
    conversation = _collapse(conversation[:conversation.index(COMMIT)])
    assert "conversations" in conversation and "messages" in conversation, conversation
    assert conversation[-1] == "entity_stats", conversation
    assert conversation.count("entity_stats") == 1, conversation
