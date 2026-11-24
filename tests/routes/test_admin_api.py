"""Admin API (/v1/organization): admin_key_required, the user detail and the user list (SQLite).

``list_pages_through_every_sort`` also runs on PostgreSQL, from
``tests/integration/test_admin_api_list_postgres.py``.
"""
from calendar import timegm
from datetime import datetime, timedelta
from http import HTTPStatus
from itertools import product
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
from lumen.models.group_member import GroupMember
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


def test_unlimited_pool_with_stale_refresh_rate_has_no_reset(app, client, target):
    # Editing a finite pool to unlimited keeps its refresh rate and balance row,
    # but the refiller never refills an unlimited pool.
    with app.app_context():
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=-2, refresh_coins=5, starting_coins=50))
        db.session.add(EntityBalance(entity_id=target["id"], coins_left=12.5, last_refill_at=datetime(2024, 11, 22, 17, 0)))
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
        # Three counted keys against two live admin/monitoring keys: the counted total and the
        # excluded total must stay different, so a flipped key-type filter changes the result.
        rows = [
            (target["id"], KEY_TYPE_NORMAL, None, None),                                 # counted
            (target["id"], KEY_TYPE_NORMAL, None, utcnow() + timedelta(days=1)),         # counted
            (target["id"], KEY_TYPE_NORMAL, None, utcnow() + timedelta(days=30)),        # counted
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
    assert _get(client, _user_url(target["uuid"])).get_json()["lumen"]["api_key_count"] == 3


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


# ---------------------------------------------------------------------------
# GET /v1/organization/users
# ---------------------------------------------------------------------------

LIST_URL = "/v1/organization/users"
LIST_LUMEN_FIELDS = {"status", "groups", "last_active_at"}
PAGE_FIELDS = {"object", "data", "first_id", "last_id", "has_more", "total"}
T0 = datetime(2025, 1, 1, 12, 0)
# (email letter, created_at, last_used_at or None when never active, active):
# ties on created_at and on last_used_at, and three never-active users.
PAGERS = [
    ("a", T0, T0 + timedelta(hours=5), True),
    ("b", T0, T0 + timedelta(hours=5), True),
    ("c", T0, None, True),
    ("d", T0 + timedelta(days=1), T0 + timedelta(hours=1), True),
    ("e", T0 + timedelta(days=1), None, False),
    ("f", T0 + timedelta(days=2), T0 + timedelta(hours=3), True),
    ("g", T0 + timedelta(days=2), None, True),
]


def _list(client, token=TOKENS["admin"], **params):
    resp = client.get(LIST_URL, query_string=params, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTPStatus.OK, resp.get_data(as_text=True)
    return resp.get_json()


def _ids(body):
    return [user["id"] for user in body["data"]]


def make_pagers(tag):
    """The PAGERS users, all matching ``search=tag``; returns {letter: (id, public id)}. Commits."""
    made = {}
    for letter, created_at, last_used_at, active in PAGERS:
        user = Entity(entity_type="user", email=f"{tag}{letter}@example.edu", name=f"{tag} {letter.upper()}",
                      active=active, created_at=created_at)
        db.session.add(user)
        db.session.flush()
        if last_used_at:
            db.session.add(EntityStat(entity_id=user.id, last_used_at=last_used_at))
        made[letter] = (user.id, f"user-{user.uuid}")
    db.session.commit()
    return made


def _expected_order(made, sort_by, sort_order):
    """The order the API must return, worked out here without SQL: ties broken by entity id."""
    desc = sort_order == "desc"
    rows = [(made[letter][0], made[letter][1], letter, created_at, last_used_at)
            for letter, created_at, last_used_at, _ in PAGERS]
    if sort_by == "last_active":
        seen = sorted((r for r in rows if r[4]), key=lambda r: (r[4], r[0]), reverse=desc)
        never = sorted((r for r in rows if not r[4]), key=lambda r: r[0], reverse=desc)
        return [r[1] for r in seen + never]
    column = 3 if sort_by == "created_at" else 2
    return [r[1] for r in sorted(rows, key=lambda r: (r[column], r[0]), reverse=desc)]


def list_pages_through_every_sort(app, client, token):
    """Paging with limit=3 over the 7 PAGERS skips and repeats nobody, in every sort_by x sort_order."""
    tag = f"pager{uuid4().hex[:8]}"
    with app.app_context():
        made = make_pagers(tag)
    for sort_by, sort_order in product(("created_at", "last_active", "email"), ("asc", "desc")):
        with app.app_context():
            limiter.reset()
        params = {"search": tag, "limit": 3, "sort_by": sort_by, "sort_order": sort_order}
        pages, after = [], None
        while True:
            body = _list(client, token, **params, **({"after": after} if after else {}))
            assert body["total"] == len(PAGERS)
            pages.append(_ids(body))
            if not body["has_more"]:
                break
            after = body["last_id"]
        seen = [user_id for page in pages for user_id in page]
        assert seen == _expected_order(made, sort_by, sort_order), (sort_by, sort_order)
        assert [len(page) for page in pages] == [3, 3, 1]


def test_list_pages_through_every_sort(app, client, keys):
    list_pages_through_every_sort(app, client, TOKENS["admin"])


def test_list_defaults_to_created_at_desc(app, client, keys):
    with app.app_context():
        made = make_pagers("dflt")
    body = _list(client, search="dflt")
    assert _ids(body) == _expected_order(made, "created_at", "desc")


def test_never_active_users_sort_last_in_both_orders(app, client, keys):
    with app.app_context():
        made = make_pagers("never")
    never = {made[letter][1] for letter, _, last_used_at, _ in PAGERS if last_used_at is None}
    for sort_order in ("asc", "desc"):
        ids = _ids(_list(client, search="never", sort_by="last_active", sort_order=sort_order))
        assert set(ids[-len(never):]) == never


def test_list_shape_has_no_coins_or_key_count(app, client, target):
    with app.app_context():
        db.session.add(EntityStat(entity_id=target["id"], last_used_at=datetime(2024, 11, 22, 18, 30),
                                  requests=7, cost=1.5))
        db.session.add(EntityLimit(entity_id=target["id"], max_coins=100, refresh_coins=5, starting_coins=50))
        db.session.commit()
        group_uuid = db.session.scalar(select(Group.uuid).where(Group.id == make_group_with_member(target["id"], "readers")))
    body = _list(client, search="ada@")
    assert set(body) == PAGE_FIELDS
    assert body["object"] == "list"
    assert body["data"] == [{
        "object": "organization.user",
        "id": f"user-{target['uuid']}",
        "name": "Ada Lovelace",
        "email": "ada@example.edu",
        "role": "reader",
        "added_at": _unix(datetime(2024, 11, 22, 16, 0)),
        "lumen": {
            "status": "active",
            "groups": [{"id": f"group-{group_uuid}", "name": "readers"}],
            "last_active_at": _unix(datetime(2024, 11, 22, 18, 30)),
        },
    }]
    assert set(body["data"][0]["lumen"]) == LIST_LUMEN_FIELDS
    assert body["first_id"] == body["last_id"] == f"user-{target['uuid']}"
    assert body["has_more"] is False
    assert body["total"] == 1


def test_list_shows_only_users(app, client, admin_user, target):
    with app.app_context():
        project = make_project("Listed Project")
        owner_uuid = str(db.session.get(Entity, project.owner_entity_id).uuid)
        project_uuid = str(project.uuid)
        ghost_uuid = _ghost_uuid(app)
        admin_uuid = str(db.session.get(Entity, admin_user["id"]).uuid)
    body = _list(client)
    assert set(_ids(body)) == {f"user-{u}" for u in (admin_uuid, target["uuid"], owner_uuid)}
    assert body["total"] == 3
    assert f"user-{project_uuid}" not in _ids(body) and f"user-{ghost_uuid}" not in _ids(body)
    admin = next(user for user in body["data"] if user["id"] == f"user-{admin_uuid}")
    assert admin["role"] == "owner"


def test_list_limit_defaults_to_20_and_allows_100(app, client, keys):
    with app.app_context():
        for i in range(21):
            db.session.add(Entity(entity_type="user", email=f"bulk{i:02d}@example.edu", name=f"Bulk {i}", active=True))
        db.session.commit()
    body = _list(client, search="bulk")
    assert len(body["data"]) == 20 and body["has_more"] is True and body["total"] == 21
    body = _list(client, search="bulk", limit=100)
    assert len(body["data"]) == 21 and body["has_more"] is False


def test_list_has_more_edges(app, client, keys):
    with app.app_context():
        made = make_pagers("edge")
    order = _expected_order(made, "created_at", "desc")
    full = _list(client, search="edge", limit=len(PAGERS))
    assert _ids(full) == order and full["has_more"] is False
    short = _list(client, search="edge", limit=len(PAGERS) - 1)
    assert short["has_more"] is True
    assert (short["first_id"], short["last_id"]) == (order[0], order[-2])
    last = _list(client, search="edge", limit=1, after=order[-2])
    assert _ids(last) == [order[-1]] and last["has_more"] is False
    assert last["first_id"] == last["last_id"] == order[-1]
    # After the very last user: an empty page, and total still counts everyone.
    empty = _list(client, search="edge", after=order[-1])
    assert empty["data"] == [] and empty["has_more"] is False
    assert empty["first_id"] is None and empty["last_id"] is None
    assert empty["total"] == len(PAGERS)


def test_list_empty_result(app, client, keys):
    body = _list(client, search="nobody-matches-this")
    assert body == {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False, "total": 0}


def test_total_applies_filters_and_ignores_after(app, client, keys):
    with app.app_context():
        made = make_pagers("tot")
    first = _list(client, search="tot", status="active", limit=2)
    assert first["total"] == 6
    rest = _list(client, search="tot", status="active", limit=2, after=first["last_id"])
    assert rest["total"] == 6
    # An after user outside the filters still marks the position.
    outside = _list(client, search="tot", status="active", after=made["e"][1])
    assert outside["total"] == 6


@pytest.fixture
def filtered(app, keys):
    """Users for the filter tests; returns {short name: public id} and the two group ids."""
    with app.app_context():
        made = {}
        for short, name, email, active in [
            ("ann", "Ann Filter", "ann@uni.example", True),
            ("bob", "Bob Filter", "bob@corp.example", True),
            ("cat", "Cat Other", "cat-filter@uni.example", False),
            ("dan", "Dan Other", "dan@uni.example", True),
        ]:
            user = Entity(entity_type="user", email=email, name=name, active=active)
            db.session.add(user)
            db.session.flush()
            made[short] = (user.id, f"user-{user.uuid}")
        db.session.commit()
        staff = make_group_with_member(made["ann"][0], name="staff")
        db.session.add_all([GroupMember(entity_id=made[short][0], group_id=staff) for short in ("cat", "dan")])
        db.session.commit()
        other = make_group_with_member(made["bob"][0], name="other")
        groups = {name: f"group-{db.session.get(Group, gid).uuid}" for name, gid in (("staff", staff), ("other", other))}
    return {short: public for short, (_, public) in made.items()}, groups


@pytest.mark.parametrize("params, expected", [
    ({"search": "filter"}, {"ann", "bob", "cat"}),           # name or email
    ({"search": "FILTER"}, {"ann", "bob", "cat"}),           # case-insensitive
    ({"search": "uni.example"}, {"ann", "cat", "dan"}),      # email only
    ({"search": "  Other "}, {"cat", "dan"}),                # trimmed, name only
    ({"group": "staff"}, {"ann", "cat", "dan"}),
    ({"group": "other"}, {"bob"}),
    ({"status": "inactive"}, {"cat"}),
    ({"search": "filter", "status": "active"}, {"ann", "bob"}),
    ({"search": "uni", "group": "staff", "status": "active"}, {"ann", "dan"}),
    ({"group": "other", "status": "inactive"}, set()),
])
def test_list_filters(client, filtered, params, expected):
    users, groups = filtered
    if "group" in params:
        params = {**params, "group": groups[params["group"]]}
    body = _list(client, **params)
    assert set(_ids(body)) == {users[short] for short in expected}
    assert body["total"] == len(expected)


# (short name, name, email local part) for search_is_literal, each prefixed with a tag.
LITERAL_USERS = [
    ("underscore", "Wild One", "wild_one"),
    ("lookalike", "Wild Xone", "wildxone"),
    ("percent", "50% Off", "pct"),
    ("fifty", "50 Plus", "fifty"),
    ("backslash", "Back\\wall", "bs"),
    ("slash", "a/b", "slash"),
    ("noslash", "ab", "noslash"),
]


def search_is_literal(app, client, token):
    """search matches a literal substring: %, _, a backslash and / are not LIKE syntax."""
    tag = f"lit{uuid4().hex[:8]}"
    made = {}
    with app.app_context():
        for short, name, local in LITERAL_USERS:
            user = Entity(entity_type="user", name=f"{tag} {name}", email=f"{tag}{local}@lit.example")
            db.session.add(user)
            db.session.flush()
            made[short] = f"user-{user.uuid}"
        db.session.commit()
    for search, expected in [
        (f"{tag}wild_one", {"underscore"}),       # _ is not a one-character wildcard
        (f"{tag} 50%", {"percent"}),              # % is not an any-string wildcard
        (f"{tag} back\\w", {"backslash"}),        # a backslash is not an escape
        (f"{tag} a/b", {"slash"}),                # nor is the autoescape character
        (f"{tag.upper()}WILD_ONE", {"underscore"}),  # still case-insensitive
    ]:
        body = _list(client, token, search=search)
        assert set(_ids(body)) == {made[short] for short in expected}, search
        assert body["total"] == len(expected), search


def test_list_search_is_literal(app, client, keys):
    search_is_literal(app, client, TOKENS["admin"])


def test_status_active_alone(app, client, filtered, admin_user):
    users, _ = filtered
    with app.app_context():
        admin_id = f"user-{db.session.get(Entity, admin_user['id']).uuid}"
    body = _list(client, status="active")
    assert set(_ids(body)) == {users["ann"], users["bob"], users["dan"], admin_id}


@pytest.mark.parametrize("param, value", [
    ("limit", "0"), ("limit", "101"), ("limit", "-1"), ("limit", "ten"), ("limit", "2.5"), ("limit", ""),
    ("sort_by", "name"), ("sort_by", ""),
    ("sort_order", "up"), ("sort_order", "DESC"),
    ("status", "blocked"), ("status", ""),
    ("after", "nope"), ("after", f"user-{uuid4()}"), ("after", "group-00000000-0000-4000-8000-000000000000"),
    ("group", "staff"), ("group", f"group-{uuid4()}"), ("group", ""),
    ("search", "ada\x00"), ("search", "\x00"),
])
def test_list_bad_parameter_is_400_with_param(client, filtered, param, value):
    resp = client.get(LIST_URL, query_string={param: value}, headers={"Authorization": f"Bearer {TOKENS['admin']}"})
    _assert_err(resp, HTTPStatus.BAD_REQUEST, "invalid_request_error")
    assert resp.get_json()["error"]["param"] == param


@pytest.mark.parametrize("make_id", [
    lambda app: f"user-{_project_uuid(app)}",
    lambda app: f"user-{_ghost_uuid(app)}",
], ids=["project", "ghost"])
def test_list_after_a_non_user_is_400(app, client, keys, make_id):
    resp = client.get(LIST_URL, query_string={"after": make_id(app)}, headers={"Authorization": f"Bearer {TOKENS['admin']}"})
    _assert_err(resp, HTTPStatus.BAD_REQUEST, "invalid_request_error")
    assert resp.get_json()["error"]["param"] == "after"


@pytest.mark.parametrize("credential", ["normal", "monitoring"])
def test_list_needs_an_admin_key(client, keys, credential):
    resp = client.get(LIST_URL, headers={"Authorization": f"Bearer {TOKENS[credential]}"})
    _assert_err(resp, HTTPStatus.FORBIDDEN, "permission_error")


def test_list_writes_no_request_logs(app, client, keys):
    with app.app_context():
        before = db.session.scalar(select(func.count()).select_from(RequestLog))
    _list(client)
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(RequestLog)) == before
