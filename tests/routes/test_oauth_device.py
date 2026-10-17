"""Device-flow tests: /oauth/device_authorization, /device, approval, /oauth/token."""
import re
from datetime import timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.auth_request import AuthRequest
from lumen.timeutils import utcnow

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@pytest.fixture(autouse=True)
def _reset_oauth_state(app):
    from lumen.extensions import limiter

    limiter.reset()
    yield
    limiter.reset()


def _issue(client, name="opencode", client_id="lumen-cli", author="alice"):
    resp = client.post("/oauth/device_authorization", data={
        "client_id": client_id, "name": name, "author": author,
    })
    assert resp.status_code == HTTPStatus.OK, resp.get_json()
    return resp


def _poll(client, body, client_id="lumen-cli"):
    return client.post("/oauth/token", data={
        "grant_type": DEVICE_GRANT, "device_code": body["device_code"], "client_id": client_id,
    })


def _request_row(app, user_code):
    with app.app_context():
        return db.session.execute(
            select(AuthRequest).where(AuthRequest.user_code == user_code)
        ).scalar_one()


def _set_expiry(app, user_code, when):
    with app.app_context():
        req = db.session.execute(
            select(AuthRequest).where(AuthRequest.user_code == user_code)
        ).scalar_one()
        req.expires_at = when
        db.session.commit()


def _approve(app, auth_client, user_code, action="approve", overwrite=None):
    # Render the consent page first: the approve POST must reference a request
    # id that was actually shown to this session.
    auth_client.get(f"/device?code={user_code}")
    data = {"request_id": _request_row(app, user_code).id, "action": action}
    if overwrite is not None:
        data["overwrite"] = "on" if overwrite else "off"
    return auth_client.post("/oauth/consent", data=data)


@pytest.mark.parametrize("data", [
    {"name": "opencode"},                                   # no client_id
    {"client_id": "lumen cli", "name": "opencode"},         # space
    {"client_id": "lumen-cli", "name": ""},                 # empty name
    {"client_id": "lumen-cli", "name": "bad/name"},         # slash in name
    {"client_id": "x" * 129, "name": "opencode"},           # too long
])
def test_device_authorization_validation(client, data):
    resp = client.post("/oauth/device_authorization", data=data)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "invalid_request"


def test_device_authorization_response_shape(client, app):
    resp = _issue(client)
    body = resp.get_json()
    assert body["device_code"]
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", body["user_code"])
    assert body["verification_uri"].endswith("/device")
    assert body["verification_uri_complete"] == f"{body['verification_uri']}?code={body['user_code']}"
    assert body["expires_in"] == 600
    assert body["interval"] == 5
    assert resp.headers["Cache-Control"] == "no-store"
    # Only the hash of the device code is stored.
    stored = str(_request_row(app, body["user_code"]).__dict__)
    assert body["device_code"] not in stored


def test_device_full_round_trip(client, auth_client, app):
    body = _issue(client).get_json()

    page = auth_client.get(f"/device?code={body['user_code']}")
    assert page.status_code == HTTPStatus.OK
    assert body["user_code"].encode() in page.data
    assert b"Unverified application" in page.data
    assert b"alice" in page.data  # author reported from the request

    approve = _approve(app, auth_client, body["user_code"])
    assert approve.status_code == HTTPStatus.OK
    assert b"Key approved" in approve.data

    token = _poll(client, body)
    assert token.status_code == HTTPStatus.OK
    key = token.get_json()["access_token"]
    assert key.startswith("sk_")
    assert token.get_json()["token_type"] == "Bearer"
    assert token.get_json()["approved_by"] == "testuser@example.com"
    assert token.headers["Cache-Control"] == "no-store"

    # The key authenticates against /v1.
    authed = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert authed.status_code == HTTPStatus.OK

    with app.app_context():
        api_key = db.session.execute(select(APIKey)).scalars().one()
        # Hash + hint stored, plaintext nowhere; provenance recorded.
        assert key not in (api_key.key_hash + (api_key.key_hint or ""))
        assert api_key.key_hint.startswith("sk_") and api_key.key_hint.endswith(key[-4:])
        assert api_key.client_id == "lumen-cli"
        assert api_key.requested_by == "alice"
        req = db.session.execute(select(AuthRequest)).scalars().one()
        assert req.status == "claimed"
        assert req.api_key_id == api_key.id


def test_poll_pending_then_slow_down(client):
    body = _issue(client).get_json()
    resp = _poll(client, body)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "authorization_pending"
    assert _poll(client, body).get_json()["error"] == "slow_down"


def test_poll_client_id_mismatch(client):
    body = _issue(client).get_json()
    assert _poll(client, body, client_id="other-cli").get_json()["error"] == "invalid_grant"


def test_poll_unknown_device_code(client):
    body = {"device_code": "nope-not-a-real-code"}
    assert _poll(client, body).get_json()["error"] == "invalid_grant"


def test_expired_pending_request(client, app):
    body = _issue(client).get_json()
    _set_expiry(app, body["user_code"], utcnow() - timedelta(seconds=1))
    assert _poll(client, body).get_json()["error"] == "expired_token"


def test_claim_window_expiry_after_approval(client, auth_client, app):
    body = _issue(client).get_json()
    assert _approve(app, auth_client, body["user_code"]).status_code == HTTPStatus.OK
    # Approval extends expiry to at least the claim window…
    assert _request_row(app, body["user_code"]).expires_at > utcnow()
    _set_expiry(app, body["user_code"], utcnow() - timedelta(seconds=1))
    assert _poll(client, body).get_json()["error"] == "expired_token"


def test_second_claim_is_denied(client, auth_client, app):
    body = _issue(client).get_json()
    assert _approve(app, auth_client, body["user_code"]).status_code == HTTPStatus.OK
    assert _poll(client, body).status_code == HTTPStatus.OK
    with app.app_context():
        _request_row(app, body["user_code"])  # ensure it exists in this ctx
        db.session.execute(
            sa_update(AuthRequest).values(last_polled_at=None)
        )
        db.session.commit()
    assert _poll(client, body).get_json()["error"] == "invalid_grant"


def test_denied_request_terminal(client, auth_client, app):
    body = _issue(client).get_json()
    deny = _approve(app, auth_client, body["user_code"], action="deny")
    assert deny.status_code == HTTPStatus.OK
    assert b"denied" in deny.data
    assert _poll(client, body).get_json()["error"] == "access_denied"


def test_device_page_requires_login_stashes_destination(client):
    resp = client.get("/device?code=ABCD-EFGH")
    assert resp.status_code == HTTPStatus.FOUND
    assert not resp.headers["Location"].startswith("http")  # never an open redirect
    with client.session_transaction() as sess:
        assert sess["oauth_return_to"] == "/device?code=ABCD-EFGH"


def test_post_login_redirect_honours_stash(client, test_user):
    with client.session_transaction() as sess:
        sess["entity_id"] = test_user["id"]
        sess["oauth_return_to"] = "/device?code=ABCD-EFGH"
    resp = client.get("/")
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["Location"] == "/device?code=ABCD-EFGH"


def test_device_page_shows_code_entry_form_when_no_code(auth_client):
    resp = auth_client.get("/device")
    assert resp.status_code == HTTPStatus.OK
    assert b"Enter confirmation code" in resp.data


def test_device_page_keeps_header_but_hides_menu(auth_client):
    resp = auth_client.get("/device")
    assert b"Log Out" in resp.data
    assert b"/groups" not in resp.data


def test_device_page_bad_code_never_locks_others_or_self(auth_client):
    # No lookup budget any more: mistypes are simply unknown codes, and the
    # same user can still see a valid consent page right afterwards.
    for _ in range(5):
        resp = auth_client.get("/device?code=ZZZZ-ZZZZ")
        assert resp.status_code == HTTPStatus.FOUND
        assert "reason=unknown_code" in resp.headers["Location"]
    body = _issue(auth_client).get_json()
    assert auth_client.get(f"/device?code={body['user_code']}").status_code == HTTPStatus.OK


def test_login_page_limits_are_per_user_not_ip(app, auth_client, admin_user):
    """Two users behind one IP (NAT, login node, or the test client) each get
    their own bucket on the login-required OAuth pages."""
    # auth_client wraps the *same* cookie jar as client, so the second user
    # needs its own test client; both still present as one IP to the app.
    user_b = app.test_client()
    with user_b.session_transaction() as sess:
        sess["entity_id"] = admin_user["id"]
    app.config["YAML_DATA"].setdefault("rate_limiting", {})["limit"] = "3 per minute"
    try:
        for _ in range(3):  # exhaust each user's bucket separately; same IP
            assert auth_client.get("/device?code=ZZZZ-ZZZZ").status_code == HTTPStatus.FOUND
            assert user_b.get("/device?code=YYYY-YYYY").status_code == HTTPStatus.FOUND
        # Both users are over *their own* limit...
        assert auth_client.get("/device?code=ZZZZ-ZZZZ").status_code == HTTPStatus.TOO_MANY_REQUESTS
        assert user_b.get("/device?code=YYYY-YYYY").status_code == HTTPStatus.TOO_MANY_REQUESTS
        # ...and a third user behind the same IP is untouched.
        user_c = app.test_client()
        with user_c.session_transaction() as sess:
            sess["entity_id"] = admin_user["id"] + 999
        assert user_c.get("/device?code=XXXX-XXXX").status_code == HTTPStatus.FOUND
    finally:
        app.config["YAML_DATA"]["rate_limiting"]["limit"] = "30 per minute"


def test_full_flow_two_users_same_ip_no_429(app, auth_client, admin_user):
    """Two different logged-in users on one IP can each complete device
    approval for their own request without being rate-limited."""
    user_b = app.test_client()
    with user_b.session_transaction() as sess:
        sess["entity_id"] = admin_user["id"]
    app.config["YAML_DATA"].setdefault("rate_limiting", {})["limit"] = "3 per minute"
    try:
        for _ in range(2):
            for c in (auth_client, user_b):
                body = _issue(c).get_json()
                page = c.get(f"/device?code={body['user_code']}")
                assert page.status_code == HTTPStatus.OK, page.status_code
                with c.session_transaction() as sess:
                    req_id = sess.get("oauth_consent_request_id")
                approve = c.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
                assert approve.status_code == HTTPStatus.OK, approve.status_code
    finally:
        app.config["YAML_DATA"]["rate_limiting"]["limit"] = "30 per minute"


def test_token_polling_many_codes_one_ip_not_limited(client):
    """A handful of CLIs behind one NAT must not starve each other: 20 codes
    polled once each stay inside the coarse IP flood guard."""
    bodies = [_issue(client).get_json() for _ in range(20)]
    for body in bodies:
        resp = _poll(client, body)
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert resp.get_json()["error"] == "authorization_pending"


def test_janitor_deletes_only_past_grace(app):
    from lumen.blueprints.oauth.routes import CLEANUP_GRACE, cleanup_expired_auth_requests

    with app.app_context():
        db.session.add_all([
            AuthRequest(
                flow="device", client_id="c", requested_name="n",
                device_code_hash="x" * 64, user_code="AAAA-BBBB",
                expires_at=utcnow() - timedelta(seconds=CLEANUP_GRACE + 300),
            ),
            AuthRequest(
                flow="device", client_id="c", requested_name="n",
                device_code_hash="y" * 64, user_code="CCCC-DDDD",
                expires_at=utcnow() - timedelta(seconds=CLEANUP_GRACE - 60),
            ),
        ])
        db.session.commit()
        assert cleanup_expired_auth_requests() == 1
        codes = {r.user_code for r in db.session.execute(select(AuthRequest)).scalars().all()}
        assert codes == {"CCCC-DDDD"}


# ---------------------------------------------------------------------------
# Key TTL requested with the device flow
# ---------------------------------------------------------------------------

def _approve_with_ttl(app, auth_client, user_code, ttl=None):
    page = auth_client.get(f"/device?code={user_code}")
    data = {"request_id": _request_row(app, user_code).id, "action": "approve"}
    if ttl is not None:
        data["ttl"] = ttl
    return page, auth_client.post("/oauth/consent", data=data)


def _minted_expires_at(app):
    with app.app_context():
        return db.session.execute(select(APIKey.expires_at)).scalar_one()


def test_device_ttl_passed_through_and_counted_from_mint(client, auth_client, app, restore_config, monkeypatch):
    app.config["KEY_TTL"] = 30 * 86400
    resp = client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": "opencode", "ttl": "7200",
    })
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_json()
    assert _request_row(app, body["user_code"]).ttl == 7200

    page, approve = _approve_with_ttl(app, auth_client, body["user_code"], ttl="7200")
    # The requested TTL is offered (even though it is not a preset) and pre-selected.
    assert b'<option value="7200" selected>2 hours</option>' in page.data
    assert approve.status_code == HTTPStatus.OK
    assert b"Key approved" in approve.data

    # Mint later than approval: the expiry counts from mint, not approval.
    minted = utcnow().replace(microsecond=0) + timedelta(seconds=30)
    monkeypatch.setattr("lumen.services.key_expiry.utcnow", lambda: minted)
    token = _poll(client, body)
    assert token.status_code == HTTPStatus.OK
    expires_at = _minted_expires_at(app)
    assert expires_at == minted + timedelta(seconds=7200)
    assert token.get_json()["expires_at"] == expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("cap", [0, 86400])
def test_device_ttl_defaults_to_cap(client, auth_client, app, restore_config, cap):
    app.config["KEY_TTL"] = cap
    body = _issue(client).get_json()
    assert _request_row(app, body["user_code"]).ttl is None

    page, approve = _approve_with_ttl(app, auth_client, body["user_code"])
    selected = b'<option value="0" selected>Never</option>' if cap == 0 else b'<option value="86400" selected>1 day</option>'
    assert selected in page.data
    assert approve.status_code == HTTPStatus.OK
    assert _request_row(app, body["user_code"]).ttl == cap

    token = _poll(client, body).get_json()
    expires_at = _minted_expires_at(app)
    if cap:
        assert abs(expires_at - (utcnow() + timedelta(seconds=cap))) < timedelta(minutes=1)
        assert token["expires_at"] == expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        assert expires_at is None
        assert "expires_at" in token and token["expires_at"] is None


@pytest.mark.parametrize("ttl", ["86401", "0", "-1", "1.5", "abc", "1d", "99999999999"])
def test_device_ttl_rejected_above_cap_or_malformed(client, app, restore_config, ttl):
    app.config["KEY_TTL"] = 86400
    resp = client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": "opencode", "ttl": ttl,
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "invalid_request"
    with app.app_context():
        assert db.session.execute(select(AuthRequest)).first() is None


@pytest.mark.parametrize("ttl", ["86401", "0", "abc", "-5"])
def test_device_tampered_consent_ttl_rejected(client, auth_client, app, restore_config, ttl):
    app.config["KEY_TTL"] = 86400
    body = _issue(client).get_json()
    _, approve = _approve_with_ttl(app, auth_client, body["user_code"], ttl=ttl)
    assert approve.status_code == HTTPStatus.OK
    assert b"Invalid expiry" in approve.data
    assert b"Key approved" not in approve.data
    assert _request_row(app, body["user_code"]).status == "pending"
    assert _poll(client, body).get_json()["error"] == "authorization_pending"


def test_device_approver_can_shorten_requested_ttl(client, auth_client, app, restore_config):
    app.config["KEY_TTL"] = 30 * 86400
    resp = client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": "opencode", "ttl": str(30 * 86400),
    })
    body = resp.get_json()
    _approve_with_ttl(app, auth_client, body["user_code"], ttl="86400")
    _poll(client, body)
    assert abs(_minted_expires_at(app) - (utcnow() + timedelta(days=1))) < timedelta(minutes=1)


def test_device_requested_ttl_over_lowered_cap_needs_new_choice(client, auth_client, app, restore_config):
    """A cap lowered after the request: the page falls back to the cap and a form without a ttl is refused."""
    app.config["KEY_TTL"] = 30 * 86400
    body = client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": "opencode", "ttl": str(7 * 86400),
    }).get_json()
    app.config["KEY_TTL"] = 86400
    page, approve = _approve_with_ttl(app, auth_client, body["user_code"])
    assert b'<option value="86400" selected>1 day</option>' in page.data
    assert b"Invalid expiry" in approve.data
    assert _request_row(app, body["user_code"]).status == "pending"
