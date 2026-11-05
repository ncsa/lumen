"""Every reader of the usage totals, after the stats cutover.

After the cutover billing no longer touches ``model_stats`` / ``entity_stats``;
the fold adds ``request_logs`` into them. Each test bills real requests through
the routes, checks the stats tables did not move, folds, and then checks that
the reader returns the totals of the requests it billed. The before-cutover
side of every reader is covered by its own route tests, which run without a
``stats_fold_state`` row.
"""
from contextlib import contextmanager
from http import HTTPStatus
from unittest.mock import patch

import pytest
from flask import template_rendered
from sqlalchemy import func, select

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.services.crypto import hash_api_key
from lumen.timeutils import as_utc_naive
from tests.conftest import fold, make_project
from tests.routes.test_api_audio import _audio_data, _mock_openai
from tests.routes.test_api_auth import _capturing_openai, _chat_post, _NonStreamResponse, fresh_rate_limit
from tests.routes.test_chat_routes import _Chunk, _fake_openai, _Usage
from tests.routes.test_preflight_queries import _own_limit

# Re-exported so pytest resolves the fixture by name in this module's tests.
__all__ = ["fresh_rate_limit"]

pytestmark = pytest.mark.usefixtures("post_cutover", "fresh_rate_limit")


def _key(app, entity_id, token):
    """An API key for ``entity_id``, which also gets a coin pool to bill against."""
    _own_limit(app, entity_id)
    with app.app_context():
        db.session.add(APIKey(entity_id=entity_id, name=token, key_hash=hash_api_key(token)))
        db.session.commit()
    return token


def _user(app, email):
    with app.app_context():
        entity = Entity(entity_type="user", email=email, name=email, initials="XX", active=True)
        db.session.add(entity)
        db.session.commit()
        return entity.id


def _bill_api(client, monkeypatch, token, model_name, n=1):
    from lumen.blueprints.api import routes
    _capturing_openai(monkeypatch, routes, lambda **kwargs: _NonStreamResponse())
    for _ in range(n):
        assert _chat_post(client, token, model_name, False).status_code == HTTPStatus.OK


def _bill_chat(auth_client, model_name):
    fake = _fake_openai([_Chunk(content="hi"), _Chunk(usage=_Usage())])
    with patch("lumen.services.llm.openai.OpenAI", fake):
        resp = auth_client.post("/chat/stream", json={
            "model": model_name, "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == HTTPStatus.OK
        resp.get_data()


def _stats(app):
    """Every running total in model_stats and entity_stats.

    ``entity_stats.conversations`` is left out, and so is a row holding no
    requests: the conversation counter stays synchronous after the cutover,
    and a user's first conversation creates their row with zero totals.
    """
    with app.app_context():
        totals = (EntityStat.requests, EntityStat.input_tokens, EntityStat.output_tokens,
                  EntityStat.cost, EntityStat.audio_seconds, EntityStat.last_used_at)
        entity = db.session.execute(select(EntityStat.entity_id, *totals).where(EntityStat.requests > 0)).all()
        model = db.session.execute(select(
            ModelStat.entity_id, ModelStat.model_config_id, ModelStat.source, ModelStat.requests,
            ModelStat.input_tokens, ModelStat.output_tokens, ModelStat.cost,
            ModelStat.audio_seconds, ModelStat.last_used_at,
        )).all()
        return sorted(map(tuple, entity)), sorted(map(tuple, model))


def _billed_and_folded(app, before):
    """Assert billing left the stats tables alone, then fold."""
    assert _stats(app) == before, "billing wrote the stats tables after the cutover"
    fold(app)
    assert _stats(app) != before, "the fold added nothing"


def _logged(app, entity_id=None, source=None):
    """(requests, tokens, cost, last time as naive UTC) from request_logs."""
    with app.app_context():
        stmt = select(
            func.count(), func.sum(RequestLog.input_tokens + RequestLog.output_tokens),
            func.sum(RequestLog.cost), func.max(RequestLog.time),
        )
        if entity_id is not None:
            stmt = stmt.where(RequestLog.entity_id == entity_id)
        if source is not None:
            stmt = stmt.where(RequestLog.source == source)
        requests, tokens, cost, last = db.session.execute(stmt).one()
        return requests, int(tokens or 0), float(cost or 0), as_utc_naive(last) if last else None


@contextmanager
def _rendered(app):
    """The context of every template rendered inside the block."""
    contexts = []

    def record(sender, template, context, **extra):
        contexts.append(context)

    template_rendered.connect(record, app)
    try:
        yield contexts
    finally:
        template_rendered.disconnect(record, app)


def test_admin_users_after_cutover(app, client, admin_client, monkeypatch, test_user, test_model, test_model_endpoint):
    """The users page header and /admin/api/users, sorted by cost and by last use."""
    other = _user(app, "other@example.com")
    busy_token = _key(app, test_user["id"], "lk_cutover_busy")
    late_token = _key(app, other, "lk_cutover_late")
    before = _stats(app)
    # test_user costs more; other is billed last.
    _bill_api(client, monkeypatch, busy_token, test_model["model_name"], n=2)
    _bill_api(client, monkeypatch, late_token, test_model["model_name"])
    _billed_and_folded(app, before)

    requests, tokens, cost, _ = _logged(app)
    with _rendered(app) as contexts:
        assert admin_client.get("/admin/users").status_code == HTTPStatus.OK
    context = contexts[0]
    assert (context["total_requests"], context["total_tokens"]) == (requests, tokens)
    assert context["total_cost"] == pytest.approx(cost)

    by_cost = admin_client.get("/admin/api/users?sort=cost&order=desc").get_json()["users"]
    assert [u["id"] for u in by_cost[:2]] == [test_user["id"], other]
    busy = by_cost[0]
    assert (busy["requests"], busy["tokens_used"]) == _logged(app, test_user["id"])[:2]
    assert busy["cost"] == pytest.approx(_logged(app, test_user["id"])[2])

    by_last_used = admin_client.get("/admin/api/users?sort=last_used&order=desc").get_json()["users"]
    assert [u["id"] for u in by_last_used[:2]] == [other, test_user["id"]]
    assert by_last_used[0]["last_used"] == _logged(app, other)[3].strftime("%Y-%m-%dT%H:%M:%SZ")


def test_projects_list_after_cutover(app, client, admin_client, monkeypatch, test_model, test_model_endpoint):
    """The /projects summary cards and the /projects/data table."""
    with app.app_context():
        busy, quiet = make_project("cutover-busy").id, make_project("cutover-quiet").id
    busy_token = _key(app, busy, "lk_cutover_project_busy")
    quiet_token = _key(app, quiet, "lk_cutover_project_quiet")
    before = _stats(app)
    _bill_api(client, monkeypatch, busy_token, test_model["model_name"], n=2)
    _bill_api(client, monkeypatch, quiet_token, test_model["model_name"])
    _billed_and_folded(app, before)

    requests, tokens, cost, _ = _logged(app)
    with _rendered(app) as contexts:
        assert admin_client.get("/projects").status_code == HTTPStatus.OK
    context = contexts[0]
    assert (context["total_requests"], context["total_tokens"]) == (requests, tokens)
    assert context["total_cost"] == pytest.approx(cost)

    rows = admin_client.get("/projects/data?sort=requests&order=desc").get_json()["projects"]
    assert [r["id"] for r in rows] == [busy, quiet]
    for row, project in zip(rows, (busy, quiet)):
        assert row["requests"] == _logged(app, project)[0]
        assert row["cost"] == pytest.approx(_logged(app, project)[2])


def test_profile_after_cutover(app, client, auth_client, monkeypatch, test_user, test_model, test_model_endpoint):
    """/profile chat stats, per-model usage and last use."""
    token = _key(app, test_user["id"], "lk_cutover_profile")  # also the chat's coin pool
    before = _stats(app)
    _bill_chat(auth_client, test_model["model_name"])
    _bill_chat(auth_client, test_model["model_name"])
    _bill_api(client, monkeypatch, token, test_model["model_name"])
    _billed_and_folded(app, before)

    with _rendered(app) as contexts:
        assert auth_client.get("/profile").status_code == HTTPStatus.OK
    context = contexts[0]

    chat_requests, chat_tokens, chat_cost, chat_last = _logged(app, test_user["id"], "chat")
    requests, input_tokens, output_tokens, cost, last_used_at = context["chat_agg"]
    assert (requests, input_tokens + output_tokens) == (chat_requests, chat_tokens)
    assert float(cost) == pytest.approx(chat_cost)
    assert last_used_at == chat_last
    assert context["conversation_count"] == 2

    all_requests, all_tokens, all_cost, all_last = _logged(app, test_user["id"])
    usage = {u["model_name"]: u for u in context["model_usage"]}[test_model["model_name"]]
    assert usage["requests"] == all_requests
    assert usage["input_tokens"] + usage["output_tokens"] == all_tokens
    assert usage["cost"] == pytest.approx(all_cost)
    assert usage["last_used_at"] == all_last
    assert context["status"]["total_tokens_used"] == all_tokens


def test_metrics_snapshot_after_cutover(app, client, monkeypatch, test_user, test_model, test_model_endpoint):
    """refresh_snapshot, the source of the /metrics usage counters."""
    from lumen.services.metrics_snapshot import refresh_snapshot
    token = _key(app, test_user["id"], "lk_cutover_metrics")
    before = _stats(app)
    _bill_api(client, monkeypatch, token, test_model["model_name"], n=3)

    with app.app_context():
        assert (test_model["model_name"], "api") not in refresh_snapshot().model_usage
    _billed_and_folded(app, before)

    requests, tokens, cost, _ = _logged(app)
    with app.app_context():
        usage = refresh_snapshot().model_usage[(test_model["model_name"], "api")]
    assert (usage.requests, usage.input_tokens + usage.output_tokens) == (requests, tokens)
    assert usage.cost == pytest.approx(cost)


def test_audio_seconds_after_cutover(app, client, test_user, test_model, test_model_endpoint):
    """/v1/audio bills audio_seconds; the fold carries them into both stats tables."""
    token = _key(app, test_user["id"], "lk_cutover_audio")
    before = _stats(app)
    payload = {"text": "hello", "usage": {"type": "duration", "seconds": 11}}
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        for _ in range(2):
            resp = client.post(
                "/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {token}"},
                data=_audio_data(model=test_model["model_name"]),
                content_type="multipart/form-data",
            )
            assert resp.status_code == HTTPStatus.OK
    _billed_and_folded(app, before)

    with app.app_context():
        assert db.session.get(EntityStat, test_user["id"]).audio_seconds == 22
        model = db.session.execute(select(ModelStat).filter_by(entity_id=test_user["id"])).scalar_one()
        assert (model.audio_seconds, model.requests) == (22, 2)
        # api_keys still count per request; they are not per-entity locks.
        assert db.session.execute(select(APIKey.audio_seconds)).scalar_one() == 22
