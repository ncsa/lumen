"""Shared consent-page tests: overwrite rules, session binding, CSRF, model ack."""
import secrets
from datetime import timedelta
from http import HTTPStatus

import bs4
import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update

from lumen.blueprints.api import routes as api_routes
from lumen.extensions import db
from lumen.models.api_key import KEY_PREFIXES, KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.services import model_cache
from lumen.services.key_expiry import expire_keys
from lumen.timeutils import utcnow
from tests.conftest import key_uuid
from tests.routes.oauth_project_helpers import project_with_member
from tests.routes.test_api_auth import _api_allows, _capturing_openai, _CompletionResponse, _grant_unlimited_pool

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
    resp = auth_client.post("/oauth/consent", data={"request_id": str(req.uuid), "action": "approve"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert b"Unknown request" in resp.data


def test_consent_csrf_enforced(client, test_user, app):
    """The consent form is CSRF-protected even though the JSON views are exempt."""
    client.application.config["WTF_CSRF_ENABLED"] = True
    try:
        with client.session_transaction() as sess:
            sess["entity_uuid"] = test_user["uuid"]
        body = _issue(client)
        with client.session_transaction() as sess:
            req_id = str(_request_row(app, body["user_code"]).uuid)
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
    assert b"Replace the secret of key" in page.data
    assert b'disabled' in page.data  # Approve rendered disabled until ticked

    req_id = str(_request_row(app, body["user_code"]).uuid)
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


def _approve_overwrite(client, auth_client, app, name="opencode"):
    """Issue a device request for name and approve it with overwrite ticked."""
    body = _issue(client, name=name)
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = str(_request_row(app, body["user_code"]).uuid)
    assert auth_client.post("/oauth/consent", data={
        "request_id": req_id, "action": "approve", "overwrite": "on",
    }).status_code == HTTPStatus.OK
    return body


def _auth_status(client, key):
    return client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code


def test_overwrite_refreshes_key(client, auth_client, app, test_user):
    old_key = _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        old = db.session.execute(select(APIKey)).scalar_one()
        old.requests, old.input_tokens, old.output_tokens, old.cost = 7, 100, 50, 1.25
        db.session.commit()
        old_id, old_hint = old.id, old.key_hint

    body = _approve_overwrite(client, auth_client, app)
    # The current secret keeps working until the application collects the new one.
    assert _auth_status(client, old_key) == HTTPStatus.OK
    new_key = _poll(client, body).get_json()["access_token"]
    assert _auth_status(client, new_key) == HTTPStatus.OK
    assert _auth_status(client, old_key) == HTTPStatus.UNAUTHORIZED

    with app.app_context():
        [key] = db.session.execute(select(APIKey).where(APIKey.name == "opencode")).scalars().all()
        # Same row, same counters, new secret, now attributed to the requesting app.
        assert key.id == old_id and key.revoked_at is None
        assert (key.requests, key.input_tokens, key.output_tokens, float(key.cost)) == (7, 100, 50, 1.25)
        assert key.key_hint != old_hint and key.key_hint == f"{new_key[:7]}...{new_key[-4:]}"
        assert (key.client_id, key.requested_by) == ("lumen-cli", "alice")
        assert _request_row(app, body["user_code"]).api_key_id == old_id


def test_overwrite_target_revoked_before_claim_mints_new_key(client, auth_client, app, test_user):
    _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        old_id = db.session.execute(select(APIKey)).scalar_one().id

    body = _approve_overwrite(client, auth_client, app)
    assert auth_client.post(f"/profile/keys/{key_uuid(app, old_id)}/revoke").status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        revoked_at = db.session.get(APIKey, old_id).revoked_at
    resp = _poll(client, body)
    assert resp.status_code == HTTPStatus.OK
    assert _auth_status(client, resp.get_json()["access_token"]) == HTTPStatus.OK

    with app.app_context():
        old = db.session.get(APIKey, old_id)
        assert old.revoked_at == revoked_at  # stays revoked, untouched
        [new] = db.session.execute(select(APIKey).where(APIKey.revoked_at.is_(None))).scalars().all()
        assert new.id != old_id and new.name == "opencode"


def test_overwrite_target_revoked_during_claim_mints_new_key(client, auth_client, app, test_user, monkeypatch):
    """A revoke landing between _mint's select and its guarded update wins: zero
    rows are refreshed and the claim falls back to minting a new key."""
    import lumen.blueprints.oauth.routes as oauth_routes
    from lumen.blueprints.profile.routes import set_key_secret

    old_key = _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        old_id = db.session.execute(select(APIKey)).scalar_one().id
    revoked_at = utcnow() - timedelta(seconds=5)
    results = []

    def revoke_then_set(api_key_id, key, **values):
        # The target was already selected as active; revoke it before the real update runs.
        db.session.execute(sa_update(APIKey).where(APIKey.id == api_key_id).values(revoked_at=revoked_at))
        results.append(set_key_secret(api_key_id, key, **values))
        return results[-1]

    monkeypatch.setattr(oauth_routes, "set_key_secret", revoke_then_set)
    body = _approve_overwrite(client, auth_client, app)
    resp = _poll(client, body)
    assert results == [False]  # the active-key guard matched zero rows
    assert resp.status_code == HTTPStatus.OK
    new_key = resp.get_json()["access_token"]
    assert _auth_status(client, new_key) == HTTPStatus.OK
    assert _auth_status(client, old_key) == HTTPStatus.UNAUTHORIZED

    with app.app_context():
        old = db.session.get(APIKey, old_id)
        assert old.revoked_at == revoked_at and old.key_hint == f"{old_key[:7]}...{old_key[-4:]}"
        [new] = db.session.execute(select(APIKey).where(APIKey.revoked_at.is_(None))).scalars().all()
        assert new.id != old_id and new.name == "opencode"
        assert new.key_hint == f"{new_key[:7]}...{new_key[-4:]}"
        assert _request_row(app, body["user_code"]).api_key_id == new.id


def test_overwrite_refreshes_newest_and_revokes_duplicates(client, auth_client, app, test_user):
    older_key = _make_manual_key(app, test_user["id"], "opencode")
    newer_key = _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        older, newer = db.session.execute(select(APIKey).order_by(APIKey.id)).scalars().all()
        older.created_at = utcnow() - timedelta(days=2)
        newer.created_at = utcnow() - timedelta(days=1)
        db.session.commit()
        older_id, newer_id = older.id, newer.id

    body = _approve_overwrite(client, auth_client, app)
    new_key = _poll(client, body).get_json()["access_token"]
    assert _auth_status(client, new_key) == HTTPStatus.OK
    assert _auth_status(client, older_key) == HTTPStatus.UNAUTHORIZED
    assert _auth_status(client, newer_key) == HTTPStatus.UNAUTHORIZED

    with app.app_context():
        older, newer = db.session.get(APIKey, older_id), db.session.get(APIKey, newer_id)
        assert newer.revoked_at is None and newer.key_hint == f"{new_key[:7]}...{new_key[-4:]}"
        assert older.revoked_at is not None and older.revoked_by_entity_id == test_user["id"]
        active = db.session.execute(
            select(APIKey).where(APIKey.name == "opencode", APIKey.revoked_at.is_(None))
        ).scalars().all()
        assert [k.id for k in active] == [newer_id]


def test_key_revoked_by_expirer_no_longer_blocks_same_name(client, auth_client, app, test_user):
    _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        old = db.session.execute(select(APIKey)).scalar_one()
        old.expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        assert expire_keys() == 1

    body = _issue(client, name="opencode")
    page = auth_client.get(f"/device?code={body['user_code']}")
    assert b"Overwrite key" not in page.data
    req_id = str(_request_row(app, body["user_code"]).uuid)
    assert auth_client.post("/oauth/consent", data={
        "request_id": req_id, "action": "approve",
    }).status_code == HTTPStatus.OK
    resp = _poll(client, body)
    assert resp.status_code == HTTPStatus.OK
    assert client.get("/v1/models", headers={
        "Authorization": f"Bearer {resp.get_json()['access_token']}",
    }).status_code == HTTPStatus.OK


@pytest.mark.parametrize("cap", [0, 86400])
def test_minted_key_expires_at_from_cap(client, auth_client, app, restore_config, cap):
    app.config["KEY_TTL"] = cap
    body = _issue(client, name="capped")
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = str(_request_row(app, body["user_code"]).uuid)
    auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert _poll(client, body).status_code == HTTPStatus.OK
    with app.app_context():
        expires_at = db.session.execute(select(APIKey.expires_at).where(APIKey.name == "capped")).scalar_one()
    if cap:
        assert abs(expires_at - (utcnow() + timedelta(seconds=cap))) < timedelta(minutes=1)
    else:
        assert expires_at is None


@pytest.mark.parametrize("cap", [0, 86400])
def test_overwrite_refresh_resets_expires_at_from_cap(client, auth_client, app, test_user, restore_config, cap):
    _make_manual_key(app, test_user["id"], "opencode")
    with app.app_context():
        old = db.session.execute(select(APIKey)).scalar_one()
        old.expires_at = utcnow() + timedelta(days=365)
        db.session.commit()
        old_id = old.id

    app.config["KEY_TTL"] = cap
    body = _approve_overwrite(client, auth_client, app)
    assert _poll(client, body).status_code == HTTPStatus.OK
    with app.app_context():
        key = db.session.get(APIKey, old_id)
    # Refreshed in place, with a new expiry counted from the claim like a newly minted key.
    assert key.revoked_at is None
    if cap:
        assert abs(key.expires_at - (utcnow() + timedelta(seconds=cap))) < timedelta(minutes=1)
    else:
        assert key.expires_at is None


def test_name_conflict_between_approval_and_mint(client, auth_client, app, test_user):
    body = _issue(client, name="opencode")
    auth_client.get(f"/device?code={body['user_code']}")
    req_id = str(_request_row(app, body["user_code"]).uuid)
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
    req_id = str(_request_row(app, body["user_code"]).uuid)
    auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    with app.app_context():
        req = db.session.execute(select(AuthRequest).where(AuthRequest.user_code == body["user_code"])).scalar_one()
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


def test_consent_page_model_ack_flow(client, auth_client, app, test_user):
    """needs_ack models offer the ack dialog; accepting records consent and the
    page then reflects the accepted state."""
    with app.app_context():
        m = ModelConfig(
            model_name="ack-model", input_cost_per_million=1.0, output_cost_per_million=1.0,
            needs_ack=True, ack_message="Be good.",
        )
        db.session.add(m)
        db.session.commit()
        model_id = m.id

    body = _issue(client)
    page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert b"ack-model" in page and b"Acknowledge" in page

    resp = auth_client.post("/profile/consent/ack-model")  # the ack dialog's POST
    assert resp.status_code == HTTPStatus.OK

    with app.app_context():
        row = db.session.execute(select(EntityModelConsent)).scalars().one()
        assert row.entity_id == test_user["id"]
        assert row.model_config_id == model_id
        assert row.consented_at is not None

    page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert b"acknowledged" in page
    import bs4
    soup = bs4.BeautifulSoup(page, "html.parser")
    assert not [b for b in soup.select(".ack-btn") if b.get("data-name") == "ack-model"]


def test_consent_page_reads_consent_rows_once(client, auth_client, app, test_user, test_model):
    """One consent-row read per page, even with a model that needs an acknowledgement."""
    from tests.routes.test_preflight_queries import CONSENT, _recording
    with app.app_context():
        db.session.add(ModelConfig(
            model_name="ack-model", input_cost_per_million=1.0, output_cost_per_million=1.0,
            needs_ack=True, ack_message="Be good.",
        ))
        db.session.commit()
    body = _issue(client)
    with _recording(app) as log:
        page = auth_client.get(f"/device?code={body['user_code']}").get_data()
    assert b"ack-model" in page and test_model["model_name"].encode() in page
    assert log.count(CONSENT) == 1


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
    req_id = str(_request_row(app, body["user_code"]).uuid)
    first = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert first.status_code == HTTPStatus.OK
    second = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert second.status_code == HTTPStatus.OK  # result page, not an error
    assert b"already" in second.get_data().lower()


@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
@pytest.mark.parametrize("overwrite", [False, True])
def test_oauth_never_overwrites_admin_or_monitoring_key(client, auth_client, app, test_user, key_type, overwrite):
    """A same-name admin or monitoring key is not an overwrite target: OAuth mints a new normal key."""
    from lumen.services.crypto import hash_api_key
    raw = KEY_PREFIXES[key_type] + "p" * 32
    with app.app_context():
        db.session.add(APIKey(entity_id=test_user["id"], name="opencode", key_hash=hash_api_key(raw),
                              key_hint="x", key_type=key_type, expires_at=utcnow() + timedelta(days=1)))
        db.session.commit()
        before = db.session.execute(select(APIKey).filter_by(name="opencode")).scalar_one()
        before = (before.id, before.key_hash, before.key_type, before.expires_at)

    body = _issue(client, name="opencode")
    page = auth_client.get(f"/device?code={body['user_code']}")
    assert b"Replace the secret of key" not in page.data
    req_id = str(_request_row(app, body["user_code"]).uuid)
    form = {"request_id": req_id, "action": "approve", **({"overwrite": "on"} if overwrite else {})}
    assert auth_client.post("/oauth/consent", data=form).status_code == HTTPStatus.OK
    assert _request_row(app, body["user_code"]).status == "approved"

    new_key = _poll(client, body).get_json()["access_token"]
    assert new_key.startswith("sk_")
    with app.app_context():
        keys = db.session.execute(select(APIKey).filter_by(name="opencode").order_by(APIKey.id)).scalars().all()
        assert len(keys) == 2
        kept, minted = keys
        assert (kept.id, kept.key_hash, kept.key_type, kept.expires_at) == before
        assert kept.revoked_at is None
        assert minted.key_type == KEY_TYPE_NORMAL and minted.key_hash == hash_api_key(new_key)


# ---------------------------------------------------------------------------
# Project key requests
# ---------------------------------------------------------------------------

def _issue_project(client, project, name="opencode"):
    resp = client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": name, "author": "alice", "project": project,
    })
    assert resp.status_code == HTTPStatus.OK
    return resp.get_json()


def _ack_models(app):
    """Three models: no ack needed, ack needed (acked by the user only), ack needed (acked by nobody)."""
    with app.app_context():
        for name, needs_ack in (("free-model", False), ("user-acked", True), ("project-acked", True)):
            db.session.add(ModelConfig(model_name=name, input_cost_per_million=1.0,
                                       output_cost_per_million=1.0, needs_ack=needs_ack,
                                       ack_message="Be good." if needs_ack else None))
        db.session.commit()
        return {m.model_name: m.id for m in db.session.execute(select(ModelConfig)).scalars()}


def _row_states(page):
    soup = bs4.BeautifulSoup(page, "html.parser")
    return {
        tr["data-model-name"]: (
            " ".join(badge.get_text().split()) if (badge := tr.select_one(".model-state .badge")) else "",
            tr.select_one(".ack-btn") is not None,
        )
        for tr in soup.select("#consent-models tbody tr[data-model-name]")
    }


def test_project_consent_page_shows_project_state(client, auth_client, app, test_user):
    ids = _ack_models(app)
    project_id, project_uuid = project_with_member(app, "Omega", test_user["id"], role="manager")
    with app.app_context():
        now = utcnow()
        db.session.add(EntityModelConsent(entity_id=test_user["id"], model_config_id=ids["user-acked"],
                                          consented_at=now))
        db.session.add(EntityModelConsent(entity_id=project_id, model_config_id=ids["project-acked"],
                                          consented_at=now))
        db.session.commit()

    body = _issue_project(client, "Omega")
    page = auth_client.get(f"/device?code={body['user_code']}").get_data(as_text=True)
    soup = bs4.BeautifulSoup(page, "html.parser")
    assert " ".join(soup.select_one("#consent-project").get_text().split()) == "Key for project Omega"
    assert soup.select_one("#consent-approved-note") is not None
    # The project's acknowledgements decide, not the approver's.
    assert _row_states(page) == {
        "free-model": ("approved", False),
        "project-acked": ("approved", False),
        "user-acked": ("not approved: a project manager must acknowledge it", True),
    }
    assert f"/projects/{project_uuid}/consent/" in page
    assert "/profile/consent/" not in page

    # The manager's ack posts to the project endpoint and the row flips.
    assert auth_client.post(f"/projects/{project_uuid}/consent/user-acked").status_code == HTTPStatus.OK
    page = auth_client.get(f"/device?code={body['user_code']}").get_data(as_text=True)
    assert _row_states(page)["user-acked"] == ("approved", False)


def test_project_consent_page_user_role_sees_no_ack_button(client, auth_client, app, test_user):
    _ack_models(app)
    project_with_member(app, "Sigma", test_user["id"], role="user")
    body = _issue_project(client, "Sigma")
    page = auth_client.get(f"/device?code={body['user_code']}").get_data(as_text=True)
    states = _row_states(page)
    assert states["user-acked"] == ("not approved: a project manager must acknowledge it", False)
    assert states["free-model"] == ("approved", False)
    assert 'class="btn btn-sm btn-outline-warning ms-1 ack-btn"' not in page


def test_personal_consent_page_unchanged(client, auth_client, app, test_user):
    _ack_models(app)
    body = _issue(client)
    page = auth_client.get(f"/device?code={body['user_code']}").get_data(as_text=True)
    assert 'id="consent-project"' not in page
    assert "/projects/" not in page
    assert _row_states(page)["user-acked"] == ("required", True)


def test_project_key_with_unapproved_model_is_refused_for_it(
    client, auth_client, app, test_user, test_model, test_model_endpoint, monkeypatch,
):
    """Approval does not wait for acknowledgements; the key cannot use the model until a manager acks it."""
    _capturing_openai(monkeypatch, api_routes, lambda **kw: _CompletionResponse())
    project_id, project_uuid = project_with_member(app, "Usage", test_user["id"], role="manager")
    with app.app_context():
        _grant_unlimited_pool(app, project_id)
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()
    model_cache.refresh(app)

    body = _issue_project(client, project_uuid)
    auth_client.get(f"/device?code={body['user_code']}")
    resp = auth_client.post("/oauth/consent", data={
        "request_id": str(_request_row(app, body["user_code"]).uuid), "action": "approve",
    })
    assert resp.status_code == HTTPStatus.OK
    token = _poll(client, body).get_json()["access_token"]

    chat = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                       json={"model": test_model["model_name"], "messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == HTTPStatus.FORBIDDEN
    assert "consent_required" in chat.get_data(as_text=True)
    assert auth_client.post(f"/projects/{project_uuid}/consent/{test_model['model_name']}").status_code == HTTPStatus.OK
    assert _api_allows(client, token, test_model["model_name"], "chat") is True
