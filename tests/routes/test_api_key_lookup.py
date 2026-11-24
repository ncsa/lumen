"""Direct tests for lookup_api_key, the Bearer-key lookup behind api_key_required."""
from datetime import timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import select

from lumen.blueprints.api.routes import APIKeyAuthFailure, lookup_api_key
from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, APIKey
from lumen.models.entity import Entity
from lumen.services.crypto import hash_api_key
from lumen.timeutils import utcnow

TOKEN = "lk_lookup_token"


@pytest.fixture
def key_id(app, test_user):
    with app.app_context():
        ak = APIKey(entity_id=test_user["id"], name="lookup-key", key_hash=hash_api_key(TOKEN))
        db.session.add(ak)
        db.session.commit()
        return ak.id


def _lookup(app, header):
    headers = {} if header is None else {"Authorization": header}
    with app.test_request_context("/v1/me", headers=headers):
        result = lookup_api_key()
        if isinstance(result, APIKeyAuthFailure):
            return result
        api_key, entity = result
        return api_key.id, entity.id


@pytest.mark.parametrize("header,message", [
    (None, "Missing or malformed Authorization header"),
    ("Basic dXNlcjpwYXNz", "Missing or malformed Authorization header"),
    ("Bearer ", "Empty token"),
])
def test_malformed_header(app, header, message):
    assert _lookup(app, header) == APIKeyAuthFailure(
        "malformed", HTTPStatus.BAD_REQUEST, message, "invalid_request_error")


def test_unknown_key(app):
    assert _lookup(app, "Bearer not-a-real-token") == APIKeyAuthFailure(
        "invalid", HTTPStatus.UNAUTHORIZED, "Invalid or revoked API key")


def test_non_ascii_token_is_invalid(app):
    with app.test_request_context("/v1/me", headers={"Authorization": "Bearer lk_caf\u00e9"}):
        assert lookup_api_key() == APIKeyAuthFailure(
            "invalid", HTTPStatus.UNAUTHORIZED, "Invalid or revoked API key")


def test_revoked_key(app, key_id):
    with app.app_context():
        db.session.get(APIKey, key_id).revoked_at = utcnow()
        db.session.commit()
    assert _lookup(app, f"Bearer {TOKEN}").kind == "invalid"


def test_expired_key_is_invalid_and_revoked(app, key_id):
    expires_at = utcnow() - timedelta(seconds=1)
    with app.app_context():
        db.session.get(APIKey, key_id).expires_at = expires_at
        db.session.commit()
    assert _lookup(app, f"Bearer {TOKEN}").kind == "invalid"
    with app.app_context():
        assert db.session.get(APIKey, key_id).revoked_at == expires_at


def test_monitor_token(app, key_id, test_user):
    monitor = "lookup-monitor-token"
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                               "api": {"monitoring": {"token": monitor}}}
    try:
        assert _lookup(app, f"Bearer {monitor}") == APIKeyAuthFailure(
            "monitor", HTTPStatus.FORBIDDEN, "Monitor token can only access /v1/models")
        # A real key still resolves while a monitor token is configured.
        assert _lookup(app, f"Bearer {TOKEN}") == (key_id, test_user["id"])
    finally:
        app.config["YAML_DATA"].pop("api", None)


def test_inactive_entity(app, key_id, test_user):
    with app.app_context():
        db.session.get(Entity, test_user["id"]).active = False
        db.session.commit()
    assert _lookup(app, f"Bearer {TOKEN}") == APIKeyAuthFailure(
        "disabled", HTTPStatus.FORBIDDEN, "Account disabled")


def test_success_returns_key_and_entity(app, key_id, test_user):
    assert _lookup(app, f"Bearer {TOKEN}") == (key_id, test_user["id"])


def test_success_bumps_no_usage_counters(app, key_id):
    _lookup(app, f"Bearer {TOKEN}")
    with app.app_context():
        key = db.session.execute(select(APIKey).filter_by(id=key_id)).scalar_one()
        assert (key.requests, key.last_used_at) == (0, None)


def _typed_key(app, owner_id, key_type):
    token = f"lk_lookup_{key_type}"
    with app.app_context():
        ak = APIKey(entity_id=owner_id, name=key_type, key_type=key_type, key_hash=hash_api_key(token))
        db.session.add(ak)
        db.session.commit()
        return token, ak.id


@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
def test_admin_owned_privileged_key_is_returned_for_the_caller_to_gate(app, admin_user, key_type):
    token, key_id = _typed_key(app, admin_user["id"], key_type)
    with app.test_request_context("/v1/me", headers={"Authorization": f"Bearer {token}"}):
        api_key, entity = lookup_api_key()
        assert (api_key.id, api_key.key_type, entity.id) == (key_id, key_type, admin_user["id"])


def test_privileged_key_of_non_admin_is_invalid(app, test_user):
    token, _ = _typed_key(app, test_user["id"], KEY_TYPE_MONITORING)
    assert _lookup(app, f"Bearer {token}") == APIKeyAuthFailure(
        "invalid", HTTPStatus.UNAUTHORIZED, "Invalid or revoked API key")
