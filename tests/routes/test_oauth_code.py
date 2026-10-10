"""Authorization-code + PKCE flow tests for /oauth/authorize and /oauth/token."""
import base64
import hashlib
import secrets
from datetime import timedelta
from http import HTTPStatus
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.auth_request import AuthRequest
from lumen.timeutils import utcnow

REDIRECT = "https://portal.example.org/lumen/callback"


@pytest.fixture(autouse=True)
def _reset_oauth_state(app):
    from lumen.extensions import limiter

    limiter.reset()
    yield
    limiter.reset()


def _new_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def _authorize_url(verifier=None, **overrides):
    if verifier is None:
        verifier, challenge = _new_pair()
    else:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
    params = {
        "response_type": "code",
        "client_id": "web-portal",
        "name": "portal-key",
        "author": "alice",
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz789",
    }
    params.update(overrides)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/oauth/authorize?{qs}", verifier


def _approve_code_flow(app, auth_client):
    """Run authorize -> consent approve; return (redirected code, verifier)."""
    url, verifier = _authorize_url()
    page = auth_client.get(url)
    assert page.status_code == HTTPStatus.OK
    assert b"portal.example.org" in page.data  # destination origin shown large
    with app.app_context():
        req_id = db.session.execute(select(AuthRequest)).scalars().one().id
    resp = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert resp.status_code == HTTPStatus.FOUND
    query = parse_qs(urlsplit(resp.headers["Location"]).query)
    assert query.get("state") == ["xyz789"]
    return query["code"][0], verifier


def test_consent_redirect_preserves_existing_query(app, auth_client):
    """Regression: an existing redirect_uri query must be re-encoded exactly
    once (?next=%2Fa stays %2Fa), a valueless flag must not be dropped, and
    code/state must be appended."""
    redirect = "https://portal.example.org/cb?next=%2Fa&flag&dup=1&dup=2"
    url, _verifier = _authorize_url(redirect_uri=quote(redirect, safe=""))
    assert auth_client.get(url).status_code == HTTPStatus.OK
    with app.app_context():
        req_id = db.session.execute(select(AuthRequest)).scalars().one().id
    resp = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert resp.status_code == HTTPStatus.FOUND
    loc = resp.headers["Location"]
    assert loc.startswith("https://portal.example.org/cb?")
    qs = loc.split("?", 1)[1]
    pairs = parse_qsl(qs, keep_blank_values=True)
    assert ("next", "/a") in pairs
    assert ("flag", "") in pairs
    assert pairs.count(("dup", "1")) == 1 and pairs.count(("dup", "2")) == 1
    assert dict(pairs).get("state") == "xyz789"
    assert any(k == "code" for k, _ in pairs)
    assert "%252F" not in qs  # never double-encoded


def _redeem(client, code, verifier, redirect=REDIRECT, client_id="web-portal"):
    return client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code, "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier,
    })


@pytest.mark.parametrize("mutate", [
    lambda p: p.update({"response_type": "token"}),
    lambda p: p.pop("client_id"),
    lambda p: p.update({"client_id": "bad client"}),
    lambda p: p.pop("name"),
    lambda p: p.update({"redirect_uri": "http://evil.example.com/cb"}),          # non-loopback http
    lambda p: p.update({"redirect_uri": "https://x.example.com/cb#frag"}),        # fragment
    lambda p: p.update({"redirect_uri": "https://user:pw@x.example.com/cb"}),     # userinfo
    lambda p: p.update({"redirect_uri": "not-a-url"}),
    lambda p: p.update({"redirect_uri": "/relative"}),
    lambda p: p.update({"code_challenge_method": "plain"}),
    lambda p: p.pop("code_challenge"),
    lambda p: p.update({"code_challenge": "short"}),
    lambda p: p.pop("state"),
    lambda p: p.update({"state": "x" * 513}),
])
def test_authorize_invalid_never_redirects(client, mutate):
    """No registry => errors render an error page; nothing redirects pre-consent."""
    url = _authorize_url()[0]
    from urllib.parse import parse_qsl, urlencode
    parsed = dict(parse_qsl(urlsplit(url).query))
    mutate(parsed)
    url = "/oauth/authorize?" + urlencode(parsed)
    resp = client.get(url)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "Location" not in resp.headers
    assert b"Invalid authorization request" in resp.data


def test_loopback_http_redirect_uri_allowed(auth_client):
    url, _ = _authorize_url(redirect_uri="http://localhost:8000/callback")
    assert auth_client.get(url).status_code == HTTPStatus.OK


def test_authorize_requires_login_stashes_query(client):
    url, _ = _authorize_url()
    resp = client.get(url)
    assert resp.status_code == HTTPStatus.FOUND
    with client.session_transaction() as sess:
        assert sess["oauth_return_to"].startswith("/oauth/authorize?")


def test_code_full_round_trip(client, auth_client, app, test_user):
    code, verifier = _approve_code_flow(app, auth_client)
    resp = _redeem(client, code, verifier)
    assert resp.status_code == HTTPStatus.OK
    key = resp.get_json()["access_token"]
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == HTTPStatus.OK
    with app.app_context():
        api_key = db.session.execute(select(APIKey)).scalars().one()
        assert api_key.client_id == "web-portal"
        assert api_key.requested_by == "alice"
        assert api_key.created_by_entity_id == test_user["id"]


def test_redeem_wrong_verifier(client, auth_client, app):
    code, _verifier = _approve_code_flow(app, auth_client)
    wrong, _ = _new_pair()
    assert _redeem(client, code, wrong).get_json()["error"] == "invalid_grant"


def test_redeem_wrong_redirect_uri(client, auth_client, app):
    code, verifier = _approve_code_flow(app, auth_client)
    resp = _redeem(client, code, verifier, redirect="https://portal.example.org/other")
    assert resp.get_json()["error"] == "invalid_grant"


def test_redeem_wrong_client_id(client, auth_client, app):
    code, verifier = _approve_code_flow(app, auth_client)
    assert _redeem(client, code, verifier, client_id="impersonator").get_json()["error"] == "invalid_grant"


def test_auth_code_ttl(client, auth_client, app):
    code, verifier = _approve_code_flow(app, auth_client)
    with app.app_context():
        req = db.session.execute(select(AuthRequest)).scalars().one()
        req.auth_code_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert _redeem(client, code, verifier).get_json()["error"] == "invalid_grant"


def test_replay_revokes_minted_key(client, auth_client, app):
    code, verifier = _approve_code_flow(app, auth_client)
    key = _redeem(client, code, verifier).get_json()["access_token"]
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == HTTPStatus.OK
    replay = _redeem(client, code, verifier)
    assert replay.get_json()["error"] == "invalid_grant"
    # RFC 6749 §4.1.2: the key minted from the leaked code is revoked.
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == HTTPStatus.UNAUTHORIZED
    # Soft delete: the row is kept, only revoked.
    with app.app_context():
        [revoked_at] = [k.revoked_at for k in db.session.execute(select(APIKey)).scalars().all()]
        assert abs(utcnow() - revoked_at) < timedelta(minutes=1)


def test_token_cors_and_no_store(client, auth_client, app):
    pre = client.options("/oauth/token")
    assert pre.status_code == HTTPStatus.NO_CONTENT
    assert pre.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in pre.headers

    code, verifier = _approve_code_flow(app, auth_client)
    resp = _redeem(client, code, verifier)
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in resp.headers
    assert resp.headers["Cache-Control"] == "no-store"


def test_unsupported_grant_type(client):
    resp = client.post("/oauth/token", data={"grant_type": "password"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "unsupported_grant_type"


def test_deny_redirects_after_consent_with_error(auth_client, app):
    url, _ = _authorize_url()
    auth_client.get(url)
    with app.app_context():
        req_id = db.session.execute(select(AuthRequest)).scalars().one().id
    resp = auth_client.post("/oauth/consent", data={"request_id": req_id, "action": "deny"})
    assert resp.status_code == HTTPStatus.FOUND
    query = parse_qs(urlsplit(resp.headers["Location"]).query)
    assert query.get("error") == ["access_denied"]
    assert query.get("state") == ["xyz789"]
    assert resp.headers["Location"].startswith("https://portal.example.org/")
