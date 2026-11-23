"""Admin API (/v1/organization): admin_key_required and the user detail endpoint (SQLite)."""
from calendar import timegm
from datetime import datetime, timedelta
from http import HTTPStatus
from uuid import uuid4

import pytest
from flask import g
from sqlalchemy import func, select

from lumen.blueprints.admin_api import routes as admin_api_routes
from lumen.extensions import db, limiter
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.lookups import parse_prefixed_id, prefixed_id, user_by_prefixed_id
from lumen.models.request_log import RequestLog
from lumen.services.crypto import hash_api_key
from lumen.timeutils import utcnow
from tests.conftest import make_group_with_member, make_project

MONITOR = "admin-api-monitor-token"
TOKENS = {
    "admin": "sk_admin_admin_api_test_key_00000000000000000",
    "normal": "sk_admin_api_test_normal_key_000000000000000",
    "monitoring": "sk_monitor_admin_api_test_key_0000000000000",
}
KEY_TYPES = {"admin": KEY_TYPE_ADMIN, "normal": KEY_TYPE_NORMAL, "monitoring": KEY_TYPE_MONITORING}
# Every field the detail may carry; anything else (an integer id, usage) fails the test.
TOP_FIELDS = {"object", "id", "name", "email", "role", "added_at", "lumen"}
LUMEN_FIELDS = {"status", "groups", "last_active_at", "coins", "api_key_count"}
COIN_FIELDS = {"balance", "allocation", "per_hour", "next_reset_at"}


def _unix(dt):
    return timegm(dt.utctimetuple())


@pytest.fixture(autouse=True)
def fresh_limits(app):
    with app.app_context():
        limiter.reset()
    yield


@pytest.fixture
def monitor_token(app, restore_config):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}), "api": {"monitoring": {"token": MONITOR}}}
    return MONITOR


@pytest.fixture
def keys(app, admin_user, monitor_token):
    """One key of each type, all owned by admin_user (in ``admins``)."""
    ids = {}
    with app.app_context():
        for name, key_type in KEY_TYPES.items():
            key = APIKey(entity_id=admin_user["id"], name=name, key_type=key_type, key_hash=hash_api_key(TOKENS[name]))
            db.session.add(key)
            db.session.flush()
            ids[name] = key.id
        db.session.commit()
    return ids


@pytest.fixture
def target(app, keys):
    """A plain user, the subject of the detail requests."""
    with app.app_context():
        user = Entity(entity_type="user", email="ada@example.edu", name="Ada Lovelace", active=True,
                      created_at=datetime(2024, 11, 22, 16, 0, 0))
        db.session.add(user)
        db.session.commit()
        return {"id": user.id, "uuid": str(user.uuid)}


def _get(client, path, token=TOKENS["admin"], headers=None):
    return client.get(path, headers=headers if headers is not None else {"Authorization": f"Bearer {token}"})


def _user_url(uuid):
    return f"/v1/organization/users/user-{uuid}"


def _assert_err(resp, status, err_type, code=None):
    assert resp.status_code == status
    body = resp.get_json()
    assert set(body) == {"error"}
    assert body["error"]["type"] == err_type
    assert body["error"]["message"]
    if code:
        assert body["error"]["code"] == code


# ---------------------------------------------------------------------------
# admin_key_required
# ---------------------------------------------------------------------------

def test_admin_key_reaches_the_view(client, target):
    assert _get(client, _user_url(target["uuid"])).status_code == HTTPStatus.OK


@pytest.mark.parametrize("headers", [{}, {"Authorization": "sk_admin_x"}, {"Authorization": "Basic abc"},
                                     {"Authorization": "Bearer "}, {"Authorization": "Bearer    "}])
def test_missing_or_malformed_header_is_400(client, target, headers):
    _assert_err(_get(client, _user_url(target["uuid"]), headers=headers), HTTPStatus.BAD_REQUEST, "invalid_request_error")


def test_non_ascii_token_is_401(client, target):
    resp = _get(client, _user_url(target["uuid"]), headers={"Authorization": "Bearer \xffabc"})
    _assert_err(resp, HTTPStatus.UNAUTHORIZED, "authentication_error")


@pytest.mark.parametrize("credential", ["normal", "monitoring"])
def test_normal_and_monitoring_keys_are_403(client, target, credential):
    resp = _get(client, _user_url(target["uuid"]), token=TOKENS[credential])
    _assert_err(resp, HTTPStatus.FORBIDDEN, "permission_error")
    assert resp.get_json()["error"]["message"] == "This endpoint requires an admin key"


def test_config_monitor_token_is_403_not_401(client, target, monkeypatch):
    # The token must be compared before resolve_api_key, which would answer 401.
    def _never(token):
        raise AssertionError("monitor token reached resolve_api_key")

    monkeypatch.setattr(admin_api_routes, "resolve_api_key", _never)
    resp = _get(client, _user_url(target["uuid"]), token=MONITOR)
    _assert_err(resp, HTTPStatus.FORBIDDEN, "permission_error")
    assert resp.get_json()["error"]["message"] == "This endpoint requires an admin key"


def test_unknown_key_is_401(client, target):
    _assert_err(_get(client, _user_url(target["uuid"]), token="sk_admin_does_not_exist"),
                HTTPStatus.UNAUTHORIZED, "authentication_error")


@pytest.mark.parametrize("change", ["revoked", "expired"])
def test_revoked_or_expired_admin_key_is_401(app, client, keys, target, change):
    with app.app_context():
        key = db.session.get(APIKey, keys["admin"])
        if change == "revoked":
            key.revoked_at = utcnow()
        else:
            key.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    _assert_err(_get(client, _user_url(target["uuid"])), HTTPStatus.UNAUTHORIZED, "authentication_error")


def test_admin_key_of_inactive_owner_is_401(app, client, admin_user, target):
    with app.app_context():
        db.session.get(Entity, admin_user["id"]).active = False
        db.session.commit()
    _assert_err(_get(client, _user_url(target["uuid"])), HTTPStatus.UNAUTHORIZED, "authentication_error")


def test_admin_key_of_owner_removed_from_admins_is_401(app, client, target, restore_config):
    app.config["YAML_DATA"] = {**app.config["YAML_DATA"], "admins": []}
    _assert_err(_get(client, _user_url(target["uuid"])), HTTPStatus.UNAUTHORIZED, "authentication_error")


def test_sets_g_to_admin_key_and_owner(app, keys, admin_user, target):
    seen = {}

    @admin_api_routes.admin_key_required
    def view():
        seen["key"], seen["entity"] = g.api_key.id, g.entity.id
        return "ok"

    with app.test_request_context(headers={"Authorization": f"Bearer {TOKENS['admin']}"}):
        assert view() == "ok"
    assert seen == {"key": keys["admin"], "entity": admin_user["id"]}


def test_rate_limited_per_key(app, client, admin_user, target, monkeypatch):
    """Each admin key has its own budget: exhausting one leaves the owner's other key usable."""
    second = "sk_admin_admin_api_second_key_000000000000000"
    with app.app_context():
        db.session.add(APIKey(entity_id=admin_user["id"], name="second", key_type=KEY_TYPE_ADMIN,
                              key_hash=hash_api_key(second)))
        db.session.commit()
    monkeypatch.setitem(app.config, "YAML_DATA", {**app.config["YAML_DATA"], "rate_limiting": {"limit": "2 per minute"}})
    for _ in range(2):
        assert _get(client, _user_url(target["uuid"])).status_code == HTTPStatus.OK
    resp = _get(client, _user_url(target["uuid"]))
    _assert_err(resp, HTTPStatus.TOO_MANY_REQUESTS, "rate_limit_error", "rate_limit_exceeded")
    assert _get(client, _user_url(target["uuid"]), token=second).status_code == HTTPStatus.OK


def test_no_request_logs_or_billing_writes(app, client, keys, admin_user, target):
    def snapshot():
        with app.app_context():
            key = db.session.get(APIKey, keys["admin"])
            return (
                db.session.scalar(select(func.count()).select_from(RequestLog)),
                db.session.scalar(select(func.count()).select_from(EntityStat)),
                db.session.scalar(select(func.count()).select_from(EntityBalance)),
                key.requests, key.input_tokens, key.output_tokens, key.cost,
            )

    before = snapshot()
    assert _get(client, _user_url(target["uuid"])).status_code == HTTPStatus.OK
    assert snapshot() == before


# ---------------------------------------------------------------------------
# GET /v1/organization/users/{user_id}
# ---------------------------------------------------------------------------

def test_detail_shape(app, client, target):
    with app.app_context():
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=100, refresh_coins=5, starting_coins=50))
        db.session.add(EntityBalance(entity_id=target["id"], coins_left=12.5, last_refill_at=datetime(2024, 11, 22, 17, 0)))
        db.session.add(EntityStat(entity_id=target["id"], last_used_at=datetime(2024, 11, 22, 18, 30)))
        db.session.commit()
    body = _get(client, _user_url(target["uuid"])).get_json()
    assert body == {
        "object": "organization.user",
        "id": f"user-{target['uuid']}",
        "name": "Ada Lovelace",
        "email": "ada@example.edu",
        "role": "reader",
        "added_at": _unix(datetime(2024, 11, 22, 16, 0)),
        "lumen": {
            "status": "active",
            "groups": [],
            "last_active_at": _unix(datetime(2024, 11, 22, 18, 30)),
            "coins": {"balance": 12.5, "allocation": 100, "per_hour": 5,
                      "next_reset_at": _unix(datetime(2024, 11, 22, 18, 0))},
            "api_key_count": 0,
        },
    }


def test_admin_is_owner_and_inactive_status(app, client, admin_user, keys):
    with app.app_context():
        user = Entity(entity_type="user", email="gone@example.edu", name="Gone", active=False)
        db.session.add(user)
        db.session.commit()
        gone = str(user.uuid)
    assert _get(client, _user_url(admin_user["uuid"])).get_json()["role"] == "owner"
    body = _get(client, _user_url(gone)).get_json()
    assert body["role"] == "reader"
    assert body["lumen"]["status"] == "inactive"
    assert body["lumen"]["last_active_at"] is None


def test_groups_lists_every_group(app, client, target):
    with app.app_context():
        b = db.session.get(Group, make_group_with_member(target["id"], name="b-group"))
        a = db.session.get(Group, make_group_with_member(target["id"], name="a-group"))
        expected = [{"id": f"group-{a.uuid}", "name": "a-group"}, {"id": f"group-{b.uuid}", "name": "b-group"}]
    assert _get(client, _user_url(target["uuid"])).get_json()["lumen"]["groups"] == expected


def test_unlimited_pool_is_minus_two(app, client, target):
    with app.app_context():
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
    coins = _get(client, _user_url(target["uuid"])).get_json()["lumen"]["coins"]
    assert coins["allocation"] == -2
    assert coins["next_reset_at"] is None


def test_blocked_pool_is_null(app, client, target):
    with app.app_context():
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=0, refresh_coins=0, starting_coins=0))
        db.session.commit()
    assert _get(client, _user_url(target["uuid"])).get_json()["lumen"]["coins"] is None


def test_pool_without_balance_row_uses_starting_coins(app, client, target):
    with app.app_context():
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=40, refresh_coins=2, starting_coins=30))
        db.session.commit()
    coins = _get(client, _user_url(target["uuid"])).get_json()["lumen"]["coins"]
    assert coins == {"balance": 30, "allocation": 40, "per_hour": 2, "next_reset_at": None}


def test_api_key_count_counts_only_usable_normal_keys(app, client, target):
    with app.app_context():
        project = make_project("Counted Not", owner_id=target["id"])
        rows = [
            (target["id"], KEY_TYPE_NORMAL, None, None),                                 # counted
            (target["id"], KEY_TYPE_NORMAL, None, utcnow() + timedelta(days=1)),         # counted
            (target["id"], KEY_TYPE_NORMAL, utcnow(), None),                             # revoked
            (target["id"], KEY_TYPE_NORMAL, None, utcnow() - timedelta(seconds=1)),      # expired
            (target["id"], KEY_TYPE_ADMIN, None, None),
            (target["id"], KEY_TYPE_MONITORING, None, None),
            (project.id, KEY_TYPE_NORMAL, None, None),                                   # project key
        ]
        for i, (eid, key_type, revoked_at, expires_at) in enumerate(rows):
            db.session.add(APIKey(entity_id=eid, key_type=key_type, key_hash=hash_api_key(f"sk_count_{i}"),
                                  revoked_at=revoked_at, expires_at=expires_at))
        db.session.commit()
    assert _get(client, _user_url(target["uuid"])).get_json()["lumen"]["api_key_count"] == 2


def test_no_integer_ids_keys_or_usage_leak(app, client, target):
    with app.app_context():
        db.session.add(APIKey(entity_id=target["id"], key_type=KEY_TYPE_NORMAL, key_hash=hash_api_key("sk_leak"),
                              key_hint="leak", requests=7, input_tokens=11, output_tokens=13, cost=1.5))
        db.session.add(EntityStat(entity_id=target["id"], requests=7, input_tokens=11, output_tokens=13, cost=1.5))
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=100, refresh_coins=5, starting_coins=50))
        db.session.commit()
        make_group_with_member(target["id"], name="leak-group")
    resp = _get(client, _user_url(target["uuid"]))
    body = resp.get_json()
    assert set(body) == TOP_FIELDS
    assert set(body["lumen"]) == LUMEN_FIELDS
    assert set(body["lumen"]["coins"]) == COIN_FIELDS
    assert all(set(grp) == {"id", "name"} for grp in body["lumen"]["groups"])
    assert body["id"].startswith("user-")
    assert all(grp["id"].startswith("group-") for grp in body["lumen"]["groups"])
    text = resp.get_data(as_text=True)
    for leaked in ("sk_leak", "leak\"", "token", "cost", "usage", "requests", "key_hash", "entity_id"):
        assert leaked not in text


@pytest.mark.parametrize("make_id", [
    lambda app, t: f"usr-{t['uuid']}",                          # bad prefix
    lambda app, t: t["uuid"],                                   # no prefix
    lambda app, t: f"group-{t['uuid']}",                        # wrong prefix
    lambda app, t: "user-not-a-uuid",
    lambda app, t: f"user-{t['uuid'].upper()}",                 # not canonical
    lambda app, t: f"user-{t['uuid'].replace('-', '')}",        # not canonical
    lambda app, t: f"user-{t['id']}",                           # integer id
    lambda app, t: f"user-{uuid4()}",                           # unknown uuid
    lambda app, t: f"user-{_project_uuid(app)}",                # a project
    lambda app, t: f"user-{_ghost_uuid(app)}",                  # the GDPR ghost
])
def test_not_a_user_is_404(app, client, target, make_id):
    resp = _get(client, f"/v1/organization/users/{make_id(app, target)}")
    _assert_err(resp, HTTPStatus.NOT_FOUND, "invalid_request_error", "user_not_found")


def _project_uuid(app):
    with app.app_context():
        return str(make_project("Not A User").uuid)


def _ghost_uuid(app):
    with app.app_context():
        ghost = Entity(entity_type="ghost", email=None, name="Ghost", initials="GH", active=False)
        db.session.add(ghost)
        db.session.commit()
        return str(ghost.uuid)


def test_cors_headers_on_admin_api(client, target):
    assert _get(client, _user_url(target["uuid"])).headers["Access-Control-Allow-Origin"] == "*"


# ---------------------------------------------------------------------------
# lookups helpers
# ---------------------------------------------------------------------------

def test_prefixed_id_round_trip(app, target):
    u = uuid4()
    assert prefixed_id("group-", u) == f"group-{u}"
    assert parse_prefixed_id(f"group-{u}", "group-") == u
    assert parse_prefixed_id(f"user-{u}", "group-") is None
    with app.app_context():
        assert user_by_prefixed_id(f"user-{target['uuid']}").id == target["id"]
        assert user_by_prefixed_id("user-x") is None
