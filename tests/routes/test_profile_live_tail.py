"""The live tail on the user's own /profile after the stats cutover.

The folded tables lag by up to ``stats.fold_lag`` plus an interval. The user's
own page adds their request_logs since ``folded_until`` on top, so it is exact
before the next fold and counts nothing twice after it. Every other reader,
including an admin viewing the same user, stays on the folded tables.
"""
from datetime import timezone
from decimal import Decimal
from http import HTTPStatus

import pytest

from lumen.blueprints.profile import routes as profile_routes
from lumen.extensions import db
from lumen.models.request_log import RequestLog
from lumen.services.stats_cutover import get_fold_state
from lumen.timeutils import utcnow
from tests.conftest import fold
from tests.routes.test_stats_after_cutover import _rendered


def _bill(app, entity_id, model_id, source, tokens=(10, 5), cost="0.5"):
    """A billed request after the cutover, stamped where the next fold will count it."""
    with app.app_context():
        at = max(utcnow(), get_fold_state().folded_until)
        db.session.add(RequestLog(
            time=at.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source=source,
            input_tokens=tokens[0], output_tokens=tokens[1], cost=Decimal(cost), duration=0.0,
        ))
        db.session.commit()
        return at


def _profile(app, client, path="/profile"):
    with _rendered(app) as contexts:
        assert client.get(path).status_code == HTTPStatus.OK
    context = contexts[0]
    chat = tuple(context["chat_agg"])
    usage = {u["model_name"]: (u["requests"], u["input_tokens"], u["output_tokens"], u["cost"], u["last_used_at"])
             for u in context["model_usage"] if u["requests"]}
    return chat, usage, context["status"]["total_tokens_used"]


@pytest.fixture
def model_id(app, test_model):
    return test_model["id"]


def test_profile_shows_requests_the_fold_has_not_reached(app, auth_client, post_cutover, test_user, test_model, model_id):
    name = test_model["model_name"]
    _bill(app, test_user["id"], model_id, "chat")
    last = _bill(app, test_user["id"], model_id, "api", tokens=(20, 10), cost="1")

    chat, usage, total_tokens = _profile(app, auth_client)
    assert chat[:3] == (1, 10, 5) and float(chat[3]) == pytest.approx(0.5)
    assert usage == {name: (2, 30, 15, pytest.approx(1.5), last)}
    assert total_tokens == 45

    # After the fold the same requests come from the tables, not twice.
    fold(app)
    assert _profile(app, auth_client) == (chat, usage, total_tokens)

    # And a request billed after that pass is in the tail again.
    newest = _bill(app, test_user["id"], model_id, "chat")
    chat, usage, total_tokens = _profile(app, auth_client)
    assert chat[:3] == (2, 20, 10) and chat[4] == newest
    assert usage == {name: (3, 40, 20, pytest.approx(2.0), newest)}
    assert total_tokens == 60


def test_profile_rereads_when_a_fold_commits_mid_read(app, auth_client, post_cutover, test_user, test_model, model_id, monkeypatch):
    """A pass after the watermark read but before the table reads moves its
    window into the tables while the tail still starts at the old watermark,
    which would count that window twice."""
    _bill(app, test_user["id"], model_id, "api")
    real = profile_routes._fetch_chat_stats
    passes = []

    def fold_then_read(eid):
        if not passes:
            passes.append(fold(app))
        return real(eid)

    monkeypatch.setattr(profile_routes, "_fetch_chat_stats", fold_then_read)
    _, usage, total_tokens = _profile(app, auth_client)
    assert passes[0] is not None
    assert usage[test_model["model_name"]][0] == 1
    assert total_tokens == 15


def test_profile_drops_the_tail_when_every_read_meets_a_fold(app, auth_client, post_cutover, test_user, test_model, model_id, monkeypatch):
    """A fold commits during every attempt, as passes do back to back while
    the fold catches up: no read validates, so the page falls back to the
    folded tables alone rather than merge a tail it knows is inconsistent."""
    real = profile_routes._fetch_chat_stats

    def bill_fold_then_read(eid):
        _bill(app, test_user["id"], model_id, "api")
        assert fold(app) is not None
        return real(eid)

    monkeypatch.setattr(profile_routes, "_fetch_chat_stats", bill_fold_then_read)
    _, usage, total_tokens = _profile(app, auth_client)
    billed = profile_routes._LIVE_TAIL_ATTEMPTS
    assert usage[test_model["model_name"]][0] == billed
    assert total_tokens == 15 * billed


def test_profile_has_no_tail_before_the_cutover(app, auth_client, test_user, model_id):
    """Before the cutover billing bumps the tables; a tail would count requests twice."""
    with app.app_context():
        db.session.add(RequestLog(time=utcnow().replace(tzinfo=timezone.utc), entity_id=test_user["id"],
                                  model_config_id=model_id, source="api", input_tokens=10, output_tokens=5,
                                  cost=Decimal("0.5"), duration=0.0))
        db.session.commit()
    _, usage, total_tokens = _profile(app, auth_client)
    assert usage == {} and total_tokens == 0


def test_admin_view_of_a_user_stays_on_the_folded_tables(app, admin_client, post_cutover, test_user, model_id):
    _bill(app, test_user["id"], model_id, "api")
    _, usage, total_tokens = _profile(app, admin_client, f"/admin/users/{test_user['id']}/profile")
    assert usage == {} and total_tokens == 0
    fold(app)
    _, usage, total_tokens = _profile(app, admin_client, f"/admin/users/{test_user['id']}/profile")
    assert total_tokens == 15
