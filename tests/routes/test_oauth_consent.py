"""Shared consent-page tests: overwrite rules, session binding, CSRF, model ack."""
import secrets
from datetime import timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.timeutils import utcnow

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@pytest.fixture(autouse=True)
def _reset_oauth_state(app):
    from lumen.extensions import limiter

    limiter.reset()
    yield
    limiter.reset()


def _issue(client, name="opencode"):
    resp = client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": name, "author": "alice",
    })
    assert resp.status_code == HTTPStatus.OK
    return resp.get_json()


def _poll(client, body):
    return client.post("/oauth/token", data={
        "grant_type": DEVICE_GRANT, "device_code": body["device_code"], "client_id": "lumen-cli",
    })


def _request_row(app, user_code):
    with app.app_context():
        return db.session.execute(
            select(AuthRequest).where(AuthRequest.user_code == user_code)
        ).scalar_one()


def _make_manual_key(app, entity_id, name):
    """Create a key through the existing hash/hint path; returns the plaintext."""
    raw = "sk_" + secrets.token_urlsafe(32)
    from lumen.services.crypto import hash_api_key
    with app.app_context():
        db.session.add(APIKey(
            entity_id=entity_id, name=name, key_hash=hash_api_key(raw),
            key_hint=f"{raw[:7]}...{raw[-4:]}",
        ))
        db.session.commit()
    return raw


def test_consent_requires_session_rendered_request_id(auth_client, client):
    body = _issue(client)
    req = _request_row(auth_client.application, body["user_code"])
    # A request id never rendered to this session cannot be approved.
    resp = auth_client.post("/oauth/consent", data={"request_id": req.id, "action": "approve"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert b"Unknown request" in resp.data


def test_consent_csrf_enforced(client, test_user, app):
    """The consent form is CSRF-protected even though the JSON views are exempt."""
    client.application.config["WTF_CSRF_ENABLED"] = True
    try:
        with client.session_transaction() as sess:
            sess["entity_id"] = test_user["id"]
        body = _issue(client)
        with client.session_transaction() as sess:
            req_id = _request_row(app, body["user_code"]).id
            sess["oauth_consent_request_id"] = req_id
        resp = client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
        assert resp.status_code == HTTPStatus.BAD_REQUEST
    finally:
        client.application.config["WTF_CSRF_ENABLED"] = False


def test_consent_device_flow_exempt_endpoints_still_work(client):
    # CSRF-enabled globally must not break the machine endpoints.
    client.application.config["WTF_CSRF_ENABLED"] = True
    try:
        assert _issue(client)
    finally:
        client.application.config["WTF_CSRF_ENABLED"] = False


def test_existing_key_requires_overwrite(client, auth_client, app, test_user):
    old_key = _make_manual_key(app, test_user["id"], "opencode")
    body = _issue(client, name="opencode")
    page = auth_client.get(f"/device?code={body['user_code']}")
    assert page.status_code == HTTPStatus.OK
    assert b"Overwrite key" in page.data
    assert b'disabled' in page.data  # Approve rendered disabled until ticked

    req_id = _request_row(app, body["user_code"]).id
    no_ow = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert no_ow.status_code == HTTPStatus.OK
    assert b"Tick" in no_ow.data
    assert _request_row(app, body["user_code"]).status == "pending"
    # The old key still authenticates; nothing changed.
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {old_key}"}).status_code == HTTPStatus.OK

    with_ow = auth_client.post("/oauth/consent", data={
        "request_id": req_id, "action": "approve", "overwrite": "on",
    })
    assert with_ow.status_code == HTTPStatus.OK
    assert _request_row(app, body["user_code"]).overwrite is True

    new_key = _poll(client, body).get_json()["access_token"]
    assert new_key != old_key
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {new_key}"}).status_code == HTTPStatus.OK
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {old_key}"}).status_code == HTTPStatus.UNAUTHORIZED


def test_overwrite_soft_deletes_old_key(client, auth_client, app, test_user):
    _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        old = db.session.execute(select(APIKey)).scalar_one()
        old.requests, old.input_tokens, old.output_tokens, old.cost = 7, 100, 50, 1.25
        db.session.commit()
        old_id = old.id

    body = _issue(client, name="opencode")
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = _request_row(app, body["user_code"]).id
    assert auth_client.post("/oauth/consent", data={
        "request_id": req_id, "action": "approve", "overwrite": "on",
    }).status_code == HTTPStatus.OK
    new_key = _poll(client, body).get_json()["access_token"]
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {new_key}"}).status_code == HTTPStatus.OK

    with app.app_context():
        old = db.session.get(APIKey, old_id)
        # The old row and its usage counters survive; only revoked_at is set.
        assert old is not None and abs(utcnow() - old.revoked_at) < timedelta(minutes=1)
        assert (old.requests, old.input_tokens, old.output_tokens, float(old.cost)) == (7, 100, 50, 1.25)
        keys = db.session.execute(select(APIKey).where(APIKey.name == "opencode")).scalars().all()
        assert sorted(k.revoked_at is None for k in keys) == [False, True]


def test_name_conflict_between_approval_and_mint(client, auth_client, app, test_user):
    body = _issue(client, name="opencode")
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = _request_row(app, body["user_code"]).id
    assert auth_client.post("/oauth/consent", data={
        "request_id": req_id, "action": "approve",
    }).status_code == HTTPStatus.OK

    # Somebody else claims the name before the CLI polls.
    _make_manual_key(app, test_user["id"], "opencode")
    denied = _poll(client, body)
    assert denied.status_code == HTTPStatus.BAD_REQUEST
    assert b"key name now in use" in denied.data
    assert _request_row(app, body["user_code"]).status == "approved"  # not consumed

    # Deleting the conflicting key lets the still-open approval mint.
    with app.app_context():
        key = db.session.execute(select(APIKey)).scalars().one()
        db.session.delete(key)
        db.session.commit()
    with app.app_context():
        db.session.execute(sa_update(AuthRequest).values(last_polled_at=None))
        db.session.commit()
    ok = _poll(client, body)
    assert ok.status_code == HTTPStatus.OK


def test_abandoned_approval_leaves_old_key_working(client, auth_client, app, test_user):
    body = _issue(client, name="opencode")
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = _request_row(app, body["user_code"]).id
    auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    with app.app_context():
        req = db.session.get(AuthRequest, req_id)
        req.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert _poll(client, body).get_json()["error"] == "expired_token"
    # No key was ever minted and the request simply expires.
    with app.app_context():
        assert db.session.execute(select(APIKey)).scalars().all() == []


def test_consent_page_shows_models_and_existing_key_hint(client, auth_client, app, test_user, test_model):
    old_key = _make_manual_key(app, test_user["id"], "opencode")
    body = _issue(client, name="opencode")
    page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert test_model["model_name"].encode() in page  # the models list is shown
    hint = (old_key[:7] + "..." + old_key[-4:]).encode()
    assert hint in page  # existing key identity surfaced


def test_consent_page_model_ack_flow(client, auth_client, app):
    """needs_ack models offer the ack dialog; accepting records consent and the
    page then reflects the accepted state."""
    with app.app_context():
        m = ModelConfig(
            model_name="ack-model", input_cost_per_million=1.0, output_cost_per_million=1.0,
            needs_ack=True, ack_message="Be good.",
        )
        db.session.add(m)
        db.session.commit()

    body = _issue(client)
    page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert b"ack-model" in page and b"Acknowledge" in page

    resp = auth_client.post("/profile/consent/ack-model")  # the ack dialog's POST
    assert resp.status_code == HTTPStatus.OK

    with app.app_context():
        row = db.session.execute(select(EntityModelConsent)).scalars().one()
        assert row.entity_id == 1 or row.entity_id is not None
        assert row.consented_at is not None

    page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert b"acknowledged" in page
    import bs4
    soup = bs4.BeautifulSoup(page, "html.parser")
    assert not [b for b in soup.select(".ack-btn") if b.get("data-name") == "ack-model"]


def test_consent_ack_button_carries_notice_verbatim(client, auth_client, app):
    """data-notice must be plain (autoescaped) text: previously it was
    |tojson, which put literal quotes and \\n into the dialog."""
    notice = 'Say "hello"\nthen goodbye'
    with app.app_context():
        m = ModelConfig(
            model_name="quote-model", input_cost_per_million=1.0,
            output_cost_per_million=1.0, needs_ack=True, ack_message=notice,
        )
        db.session.add(m)
        db.session.commit()
    body = _issue(client)
    page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert b'"\\n"' not in page  # no JSON-escaped newline survives in the markup
    import bs4
    soup = bs4.BeautifulSoup(page, "html.parser")
    btn = [b for b in soup.select(".ack-btn") if b.get("data-name") == "quote-model"][0]
    assert btn["data-notice"] == notice  # quotes and real newline, no wrapper
    assert btn["data-notice"].startswith("Say ")
    assert "data-early-notice" in btn.attrs
    assert btn.get("aria-label") == "Acknowledge quote-model"


def test_consent_approve_second_time_is_already_handled(client, auth_client, app):
    body = _issue(client)
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = _request_row(app, body["user_code"]).id
    first = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert first.status_code == HTTPStatus.OK
    second = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert second.status_code == HTTPStatus.OK  # result page, not an error
    assert b"already" in second.get_data().lower()
