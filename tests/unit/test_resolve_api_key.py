"""Unit tests for ``resolve_api_key``: every outcome, and the last_used_at limit."""
from datetime import datetime, timedelta
from http import HTTPStatus

import pytest
from sqlalchemy.exc import IntegrityError

from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.models.entity import Entity
from lumen.services import api_auth
from lumen.services.api_auth import ACCOUNT_DISABLED, INVALID_KEY, KeyRejected, resolve_api_key
from lumen.services.crypto import hash_api_key

NOW = datetime(2026, 11, 22, 12, 0, 0)
ALL_TYPES = (KEY_TYPE_NORMAL, KEY_TYPE_ADMIN, KEY_TYPE_MONITORING)
PRIVILEGED = (KEY_TYPE_ADMIN, KEY_TYPE_MONITORING)


@pytest.fixture
def clock(monkeypatch):
    """A frozen, settable clock for api_auth (its last_used_at limit)."""
    now = {"t": NOW}
    monkeypatch.setattr(api_auth, "utcnow", lambda: now["t"])
    return now


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


def _key(owner_id, key_type, **fields):
    token = f"sk_unit_{key_type}_{owner_id}_{len(fields)}"
    key = APIKey(entity_id=owner_id, name=key_type, key_type=key_type, key_hash=hash_api_key(token), **fields)
    db.session.add(key)
    db.session.commit()
    return token, key.id


def _set(model, pk, **fields):
    row = db.session.get(model, pk)
    for name, value in fields.items():
        setattr(row, name, value)
    db.session.commit()


def test_unknown_token(ctx, admin_user):
    assert resolve_api_key("sk_nope") is INVALID_KEY
    assert INVALID_KEY == KeyRejected("Invalid or revoked API key", HTTPStatus.UNAUTHORIZED)


@pytest.mark.parametrize("key_type", ALL_TYPES)
def test_usable_key_returns_key_and_owner(ctx, admin_user, key_type):
    token, key_id = _key(admin_user["id"], key_type)
    key, owner = resolve_api_key(token)
    assert (key.id, key.key_type, owner.id) == (key_id, key_type, admin_user["id"])


@pytest.mark.parametrize("key_type", ALL_TYPES)
def test_revoked_key(ctx, admin_user, key_type):
    token, _ = _key(admin_user["id"], key_type, revoked_at=datetime(2026, 1, 1))
    assert resolve_api_key(token) is INVALID_KEY


@pytest.mark.parametrize("key_type", ALL_TYPES)
def test_expired_key_is_revoked(ctx, admin_user, key_type):
    token, key_id = _key(admin_user["id"], key_type, expires_at=datetime(2026, 1, 1))
    assert resolve_api_key(token) is INVALID_KEY
    assert db.session.get(APIKey, key_id).revoked_at == datetime(2026, 1, 1)


def test_normal_key_of_inactive_owner_is_disabled(ctx, test_user):
    token, _ = _key(test_user["id"], KEY_TYPE_NORMAL)
    _set(Entity, test_user["id"], active=False)
    assert resolve_api_key(token) is ACCOUNT_DISABLED
    assert ACCOUNT_DISABLED.status == HTTPStatus.FORBIDDEN


def test_normal_key_does_not_need_an_admin_owner(ctx, test_user):
    token, _ = _key(test_user["id"], KEY_TYPE_NORMAL)
    assert not isinstance(resolve_api_key(token), KeyRejected)


@pytest.mark.parametrize("key_type", PRIVILEGED)
def test_privileged_key_of_non_admin_owner(ctx, test_user, key_type):
    token, _ = _key(test_user["id"], key_type)
    assert resolve_api_key(token) is INVALID_KEY


@pytest.mark.parametrize("key_type", PRIVILEGED)
def test_privileged_key_of_inactive_admin(ctx, admin_user, key_type):
    token, _ = _key(admin_user["id"], key_type)
    _set(Entity, admin_user["id"], active=False)
    assert resolve_api_key(token) is INVALID_KEY
    _set(Entity, admin_user["id"], active=True)
    assert not isinstance(resolve_api_key(token), KeyRejected)


@pytest.mark.parametrize("key_type", PRIVILEGED)
def test_privileged_key_works_again_when_owner_is_restored(app, ctx, admin_user, key_type):
    token, _ = _key(admin_user["id"], key_type)
    admins = app.config["YAML_DATA"]["admins"]
    app.config["YAML_DATA"]["admins"] = []
    try:
        assert resolve_api_key(token) is INVALID_KEY
    finally:
        app.config["YAML_DATA"]["admins"] = admins
    assert not isinstance(resolve_api_key(token), KeyRejected)


@pytest.mark.parametrize("key_type", PRIVILEGED)
def test_last_used_at_is_written_at_most_once_a_minute(ctx, admin_user, clock, key_type):
    token, key_id = _key(admin_user["id"], key_type)

    resolve_api_key(token)
    assert db.session.get(APIKey, key_id).last_used_at == NOW

    clock["t"] = NOW + timedelta(seconds=59)
    resolve_api_key(token)
    assert db.session.get(APIKey, key_id).last_used_at == NOW

    clock["t"] = NOW + timedelta(minutes=1)
    resolve_api_key(token)
    assert db.session.get(APIKey, key_id).last_used_at == NOW + timedelta(minutes=1)

    clock["t"] = NOW + timedelta(minutes=3)
    resolve_api_key(token)
    assert db.session.get(APIKey, key_id).last_used_at == NOW + timedelta(minutes=3)


def test_last_used_at_is_not_written_for_a_refused_owner(ctx, test_user, clock):
    token, key_id = _key(test_user["id"], KEY_TYPE_ADMIN)
    resolve_api_key(token)
    assert db.session.get(APIKey, key_id).last_used_at is None


def test_normal_key_last_used_at_is_left_to_billing(ctx, test_user, clock):
    token, key_id = _key(test_user["id"], KEY_TYPE_NORMAL)
    resolve_api_key(token)
    assert db.session.get(APIKey, key_id).last_used_at is None


def test_key_type_defaults_to_normal(ctx, test_user):
    key = APIKey(entity_id=test_user["id"], name="plain", key_hash=hash_api_key("sk_plain_default"))
    db.session.add(key)
    db.session.commit()
    assert key.key_type == KEY_TYPE_NORMAL


def test_key_type_check_constraint(ctx, test_user):
    db.session.add(APIKey(entity_id=test_user["id"], name="bad", key_type="root",
                          key_hash=hash_api_key("sk_bad_type")))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
