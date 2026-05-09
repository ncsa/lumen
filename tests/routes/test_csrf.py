"""Tests verifying CSRF protection is enforced on form routes and exempt on the API."""
import pytest
from bs4 import BeautifulSoup


@pytest.fixture
def csrf_client(app, test_user):
    """Authenticated test client with CSRF protection enabled."""
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client(use_cookies=True)
    with client.session_transaction() as sess:
        sess["entity_id"] = test_user["id"]
        sess["entity_name"] = test_user["name"]
        sess["initials"] = test_user["initials"]
        sess["gravatar_hash"] = test_user["gravatar_hash"]
    yield client
    app.config["WTF_CSRF_ENABLED"] = False


def _extract_csrf_token(resp):
    soup = BeautifulSoup(resp.data, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    assert meta, "No csrf-token meta tag found in response"
    return meta["content"]


def _setup_graylist(app, entity_id, model_config_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=entity_id,
            model_config_id=model_config_id,
            access_type="graylist",
        ))
        db.session.commit()


def test_consent_rejected_without_csrf_token(app, csrf_client, test_model, test_user):
    _setup_graylist(app, test_user["id"], test_model["id"])
    resp = csrf_client.post(f"/models/{test_model['model_name']}/consent")
    assert resp.status_code == 400
    assert b"CSRF" in resp.data


def test_consent_accepted_with_csrf_token(app, csrf_client, test_model, test_user):
    _setup_graylist(app, test_user["id"], test_model["id"])
    detail = csrf_client.get(f"/models/{test_model['model_name']}")
    token = _extract_csrf_token(detail)
    resp = csrf_client.post(
        f"/models/{test_model['model_name']}/consent",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_delete_conversation_rejected_without_csrf_token(app, csrf_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        conv = Conversation(entity_id=test_user["id"], title="To Delete", model="m")
        db.session.add(conv)
        db.session.commit()
        conv_id = conv.id

    resp = csrf_client.delete(f"/chat/conversations/{conv_id}")
    assert resp.status_code == 400
    assert b"CSRF" in resp.data


def test_delete_conversation_accepted_with_csrf_token(app, csrf_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        conv = Conversation(entity_id=test_user["id"], title="To Delete", model="m")
        db.session.add(conv)
        db.session.commit()
        conv_id = conv.id

    chat = csrf_client.get("/chat")
    token = _extract_csrf_token(chat)
    resp = csrf_client.delete(
        f"/chat/conversations/{conv_id}",
        headers={"X-CSRFToken": token},
    )
    assert resp.status_code == 200


def test_api_blueprint_exempt_from_csrf(csrf_client):
    """The /v1/ API blueprint must not require CSRF tokens."""
    resp = csrf_client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        content_type="application/json",
    )
    # Auth fails (401) — CSRF must not be the reason (400 + CSRF body)
    assert resp.status_code != 400 or b"CSRF" not in resp.data


def test_stream_rejected_without_csrf_token(csrf_client, test_model):
    """/chat/stream without a CSRF token must be rejected."""
    resp = csrf_client.post(
        "/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "model": test_model["model_name"]},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert b"CSRF" in resp.data


def test_stream_not_rejected_with_csrf_token(app, csrf_client, test_model, test_user):
    """/chat/stream with a valid X-CSRFToken header must not be blocked by CSRF."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()

    chat = csrf_client.get("/chat")
    token = _extract_csrf_token(chat)
    resp = csrf_client.post(
        "/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "model": test_model["model_name"]},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    # CSRF passes; fails for another reason (no healthy endpoint), not a CSRF 400
    assert not (resp.status_code == 400 and b"CSRF" in resp.data)
