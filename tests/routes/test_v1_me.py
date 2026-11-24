"""GET /v1/me on SQLite: auth, response shape, template coverage and periods.

Month usage needs PostgreSQL (``tests/integration/test_v1_me_postgres.py``);
here a month answers ``usage_available: false``.
"""
import json
import re
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from lumen.blueprints.profile.routes import _get_profile_data
from lumen.extensions import db, limiter
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.entity import Entity
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_stat import EntityStat
from lumen.models.model_stat import ModelStat
from lumen.services.crypto import hash_api_key
from lumen.timeutils import utcnow
from tests.conftest import make_group_with_member, make_project
from tests.routes.test_oauth_code import _authorize_url, _redeem
from tests.routes.test_oauth_device import _approve, _issue, _poll

KEY = "sk_" + "a" * 43
OTHER_KEY = "sk_" + "b" * 43

MALFORMED = "Missing or malformed Authorization header; expected 'Bearer <key>'"
INVALID = "Invalid or revoked API key"
UNAUTHENTICATED = "Authentication required: sign in or send 'Authorization: Bearer <key>'"
PROJECT_KEY = "Project keys cannot access /v1/me; use the web session or a personal API key"
OAUTH_KEY = "Keys issued through OAuth cannot access /v1/me; create a personal API key on your profile page"


@pytest.fixture(autouse=True)
def _reset_limiter():
    # The OAuth endpoints are rate limited; the flows below must not trip that.
    limiter.reset()
    yield
    limiter.reset()


def _add_key(entity_id, raw=KEY, **fields):
    fields.setdefault("name", "personal")
    ak = APIKey(entity_id=entity_id, key_hash=hash_api_key(raw),
                key_hint=f"{raw[:7]}...{raw[-4:]}", **fields)
    db.session.add(ak)
    db.session.commit()
    return ak.id


@pytest.fixture
def key_id(app, test_user):
    with app.app_context():
        return _add_key(test_user["id"], created_by_entity_id=test_user["id"])


def _uuid(app, model, pk):
    """The public uuid of row ``pk``, as /v1/me reports it."""
    with app.app_context():
        return str(db.session.get(model, pk).uuid)


def _bearer(raw=KEY):
    return {"Authorization": f"Bearer {raw}"}


def _assert_error(resp, status, err_type, message):
    assert resp.status_code == status, resp.get_data(as_text=True)
    assert resp.get_json() == {"error": {"message": message, "type": err_type}}


@pytest.fixture
def seeded(app, test_user, test_model, test_model_endpoint, admin_user):
    """A user with something in every section: group, pool, project, keys, usage."""
    eid = test_user["id"]
    with app.app_context():
        make_group_with_member(eid, name="physics")
        db.session.add(EntityLimit(entity_id=eid, max_coins=100, refresh_coins=10, starting_coins=50))
        project = make_project("Lab Project", owner_id=eid)
        now = utcnow().replace(microsecond=0)
        db.session.add(ModelStat(entity_id=eid, model_config_id=test_model["id"], source="chat",
                                 requests=4, input_tokens=100, output_tokens=200,
                                 cost=Decimal("0.5"), last_used_at=now - timedelta(hours=1)))
        db.session.add(ModelStat(entity_id=eid, model_config_id=test_model["id"], source="api",
                                 requests=2, input_tokens=10, output_tokens=20,
                                 cost=Decimal("0.25"), last_used_at=now))
        db.session.add(EntityStat(entity_id=eid, requests=6, input_tokens=110, output_tokens=220,
                                  cost=Decimal("0.75"), conversations=3))
        db.session.add(EntityStat(entity_id=project.id, requests=9, input_tokens=5, output_tokens=6,
                                  cost=Decimal("1.5")))
        key = _add_key(eid, created_by_entity_id=eid, requests=2, input_tokens=10, output_tokens=20,
                       cost=Decimal("0.25"), last_used_at=now, expires_at=now + timedelta(days=30))
        revoked = _add_key(eid, OTHER_KEY, name="old", created_by_entity_id=eid,
                           revoked_at=now - timedelta(days=1), revoked_by_entity_id=admin_user["id"])
        return {"eid": eid, "key": key, "revoked": revoked, "project": project.id,
                "model": test_model["model_name"]}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_session_and_key_return_the_same_body(app, seeded, auth_client, client):
    by_session = auth_client.get("/v1/me?period=all")
    assert by_session.status_code == HTTPStatus.OK
    with client.session_transaction() as sess:
        sess.clear()
    by_key = client.get("/v1/me?period=all", headers=_bearer())
    assert by_key.status_code == HTTPStatus.OK
    assert by_session.get_json() == by_key.get_json()
    assert by_key.get_json()["user"]["id"] == _uuid(app, Entity, seeded["eid"])
    # Private data: neither mode may be stored by a cache.
    assert by_session.headers["Cache-Control"] == "no-store"
    assert by_key.headers["Cache-Control"] == "no-store"


def test_key_wins_over_session(app, key_id, test_user, admin_client):
    resp = admin_client.get("/v1/me", headers=_bearer())
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["user"]["id"] == _uuid(app, Entity, test_user["id"])


def test_bad_key_with_a_valid_session_is_refused(auth_client):
    _assert_error(auth_client.get("/v1/me", headers=_bearer("sk_nope")),
                  HTTPStatus.UNAUTHORIZED, "authentication_error", INVALID)


@pytest.mark.parametrize("header", ["", "Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "bearer " + KEY, KEY])
def test_malformed_header_is_401(client, key_id, header):
    _assert_error(client.get("/v1/me", headers={"Authorization": header}),
                  HTTPStatus.UNAUTHORIZED, "authentication_error", MALFORMED)


@pytest.mark.parametrize("change", ["unknown", "revoked", "expired"])
def test_invalid_key_is_401(app, client, key_id, change):
    with app.app_context():
        key = db.session.get(APIKey, key_id)
        if change == "revoked":
            key.revoked_at = utcnow()
        elif change == "expired":
            key.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    raw = "sk_unknown" if change == "unknown" else KEY
    _assert_error(client.get("/v1/me", headers=_bearer(raw)),
                  HTTPStatus.UNAUTHORIZED, "authentication_error", INVALID)


def test_no_credentials_is_401_not_a_redirect(client):
    resp = client.get("/v1/me")
    assert "Location" not in resp.headers
    _assert_error(resp, HTTPStatus.UNAUTHORIZED, "authentication_error", UNAUTHENTICATED)


@pytest.mark.parametrize("problem", ["inactive", "missing"])
def test_session_without_an_active_entity_is_401(app, auth_client, test_user, problem):
    with app.app_context():
        entity = db.session.get(Entity, test_user["id"])
        if problem == "inactive":
            entity.active = False
        else:
            db.session.delete(entity)
        db.session.commit()
    resp = auth_client.get("/v1/me")
    assert "Location" not in resp.headers
    _assert_error(resp, HTTPStatus.UNAUTHORIZED, "authentication_error", UNAUTHENTICATED)


def test_three_401_messages_are_distinct():
    assert len({MALFORMED, INVALID, UNAUTHENTICATED}) == 3


def test_monitor_token_is_403(app, client, restore_config):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                               "api": {"monitoring": {"token": "monitor-secret"}}}
    _assert_error(client.get("/v1/me", headers=_bearer("monitor-secret")),
                  HTTPStatus.FORBIDDEN, "permission_error", "Monitor token cannot access /v1/me")


@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
def test_admin_and_monitoring_keys_are_403(app, client, auth_client, admin_user, restore_config, key_type):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}), "admins": ["admin@example.com"]}
    raw = f"sk_{key_type}_" + "c" * 40
    with app.app_context():
        _add_key(admin_user["id"], raw=raw, key_type=key_type, created_by_entity_id=admin_user["id"])
    expected = f"{key_type.capitalize()} keys can only access /v1/models"
    # The key decides even with a signed-in session.
    for c in (client, auth_client):
        _assert_error(c.get("/v1/me", headers=_bearer(raw)),
                      HTTPStatus.FORBIDDEN, "permission_error", expected)


def test_inactive_entity_key_is_403(app, client, key_id, test_user):
    with app.app_context():
        db.session.get(Entity, test_user["id"]).active = False
        db.session.commit()
    _assert_error(client.get("/v1/me", headers=_bearer()),
                  HTTPStatus.FORBIDDEN, "authentication_error", "Account disabled")


def test_project_key_is_403(app, client, test_user):
    with app.app_context():
        project = make_project("Keyed Project", owner_id=test_user["id"])
        _add_key(project.id, created_by_entity_id=test_user["id"])
    _assert_error(client.get("/v1/me", headers=_bearer()),
                  HTTPStatus.FORBIDDEN, "permission_error", PROJECT_KEY)


def _device_flow_key(app, client, auth_client, overwrite=None):
    body = _issue(client, name="personal").get_json()
    assert _approve(app, auth_client, body["user_code"], overwrite=overwrite).status_code == HTTPStatus.OK
    resp = _poll(client, body)
    assert resp.status_code == HTTPStatus.OK, resp.get_json()
    return resp.get_json()["access_token"]


def _code_flow_key(app, client, auth_client, overwrite=False):
    url, verifier = _authorize_url(name="personal")
    assert auth_client.get(url).status_code == HTTPStatus.OK
    with app.app_context():
        req_id = str(db.session.execute(select(AuthRequest.uuid)).scalar_one())
    data = {"request_id": req_id, "action": "approve"}
    if overwrite:
        data["overwrite"] = "on"
    resp = auth_client.post("/oauth/consent", data=data)
    code = parse_qs(urlsplit(resp.headers["Location"]).query)["code"][0]
    resp = _redeem(client, code, verifier)
    assert resp.status_code == HTTPStatus.OK, resp.get_json()
    return resp.get_json()["access_token"]


def _forbidden_as_oauth(app, client, raw):
    # Signed out, so only the key can authenticate.
    with client.session_transaction() as sess:
        sess.clear()
    _assert_error(client.get("/v1/me", headers=_bearer(raw)),
                  HTTPStatus.FORBIDDEN, "permission_error", OAUTH_KEY)


def test_device_flow_key_is_403(app, client, auth_client):
    _forbidden_as_oauth(app, client, _device_flow_key(app, client, auth_client))


def test_code_flow_key_is_403(app, client, auth_client):
    _forbidden_as_oauth(app, client, _code_flow_key(app, client, auth_client))


def test_web_key_overwritten_through_oauth_is_403(app, client, auth_client, key_id):
    # Before the overwrite the web key works.
    assert client.get("/v1/me", headers=_bearer()).status_code == HTTPStatus.OK
    raw = _code_flow_key(app, client, auth_client, overwrite=True)
    with app.app_context():
        [row] = db.session.execute(select(APIKey)).scalars().all()
        assert row.id == key_id and row.client_id == "web-portal"
    _forbidden_as_oauth(app, client, raw)


def test_oauth_key_rotated_in_the_ui_stays_403(app, client, auth_client):
    _device_flow_key(app, client, auth_client)
    with app.app_context():
        kid = db.session.execute(select(APIKey.uuid)).scalar_one()
    rotated = "sk_" + secrets.token_urlsafe(32)
    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": rotated})
    assert resp.status_code == HTTPStatus.OK, resp.get_json()
    _forbidden_as_oauth(app, client, rotated)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

SECTIONS = {"user", "period", "totals", "model_usage", "model_access", "coin_pool",
            "projects", "api_keys", "lifetime"}


def _all_values(node, path=""):
    """Every (path, key, value) in a JSON document."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k, v
            yield from _all_values(v, f"{path}.{k}")
    elif isinstance(node, list):
        for v in node:
            yield from _all_values(v, f"{path}[]")


def test_every_section_is_present(seeded, client):
    body = client.get("/v1/me?period=all", headers=_bearer()).get_json()
    assert set(body) == SECTIONS
    assert body["coin_pool"]["coin_limit"] == 100
    assert body["user"]["groups"] == ["physics"]
    assert [p["name"] for p in body["projects"]] == ["Lab Project"]
    assert body["projects"][0]["lifetime"] == {"requests": 9, "tokens": 11, "cost": 1.5}
    assert body["lifetime"]["conversation_count"] == 3
    assert body["lifetime"]["chat"]["requests"] == 4


def test_coin_pool_is_null_without_a_pool(app, client, key_id, test_user):
    with app.app_context():
        # max_coins 0 blocks the entity: no pool.
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=0, refresh_coins=0, starting_coins=0))
        db.session.commit()
    body = client.get("/v1/me", headers=_bearer()).get_json()
    assert set(body) == SECTIONS
    assert body["coin_pool"] is None


def test_revoked_keys_are_listed(app, seeded, client):
    keys = {k["id"]: k for k in client.get("/v1/me", headers=_bearer()).get_json()["api_keys"]}
    key, revoked = _uuid(app, APIKey, seeded["key"]), _uuid(app, APIKey, seeded["revoked"])
    assert set(keys) == {key, revoked}
    assert keys[revoked]["revoked_at"].endswith("Z")
    assert keys[revoked]["revoked_by"] == "Admin User"
    assert keys[key]["revoked_at"] is None
    assert keys[key]["created_by"] == "Test User"


def test_ids_are_public_uuids(app, seeded, client):
    """Every id in the body is the row's uuid, never its integer primary key."""
    body = client.get("/v1/me?period=all", headers=_bearer()).get_json()
    assert body["user"]["id"] == _uuid(app, Entity, seeded["eid"])
    assert [p["id"] for p in body["projects"]] == [_uuid(app, Entity, seeded["project"])]
    for path, key, value in _all_values(body):
        if key == "id":
            assert isinstance(value, str) and str(uuid.UUID(value)) == value, (path, value)


def test_no_key_hash_or_secret(seeded, client):
    text = client.get("/v1/me?period=all", headers=_bearer()).get_data(as_text=True)
    assert "key_hash" not in text
    for raw in (KEY, OTHER_KEY):
        assert raw not in text
        assert hash_api_key(raw) not in text


_TIMESTAMP = re.compile(r"_at$|^(start|end|next_refill)$")


def test_every_timestamp_ends_in_z(app, seeded, client):
    with app.app_context():
        from lumen.models.entity_balance import EntityBalance
        db.session.add(EntityBalance(entity_id=seeded["eid"], coins_left=Decimal("12.5"),
                                     last_refill_at=utcnow()))
        db.session.commit()
    for period in ("all", "2026-01"):
        body = client.get(f"/v1/me?period={period}", headers=_bearer()).get_json()
        stamps = [(p, k, v) for p, k, v in _all_values(body) if _TIMESTAMP.search(k) and v is not None]
        assert stamps, body
        for p, k, v in stamps:
            assert isinstance(v, str) and v.endswith("Z") and "+" not in v, (p, k, v)
    assert body["coin_pool"]["next_refill"].endswith("Z")


def test_cors_headers(seeded, client):
    resp = client.get("/v1/me", headers=_bearer())
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in resp.headers
    assert client.get("/v1/me").headers["Access-Control-Allow-Origin"] == "*"


def test_key_call_bumps_no_counters(app, seeded, client):
    def snapshot():
        with app.app_context():
            k = db.session.get(APIKey, seeded["key"])
            return (k.requests, k.input_tokens, k.output_tokens, k.cost, k.last_used_at)
    before = snapshot()
    assert client.get("/v1/me", headers=_bearer()).status_code == HTTPStatus.OK
    assert snapshot() == before


_LIFETIME_COUNTERS = {"requests", "input_tokens", "output_tokens", "total_tokens", "tokens", "cost",
                      "conversation_count"}


def test_no_lifetime_counter_outside_lifetime(seeded, client):
    body = client.get("/v1/me", headers=_bearer()).get_json()
    assert "conversation_count" not in json.dumps(body["totals"] or {})
    # Counters are allowed in the period sections (totals, model_usage) and under
    # a lifetime key; anywhere else one would mix lifetime and period values.
    for path, key, _ in _all_values(body):
        if key in _LIFETIME_COUNTERS:
            assert ".lifetime" in path or path.startswith((".totals", ".model_usage")), (path, key)


def test_admin_mode_is_reported_on_session_calls_only(admin_client, app, admin_user):
    with app.app_context():
        _add_key(admin_user["id"])
    by_session = admin_client.get("/v1/me").get_json()["user"]
    assert by_session["admin_eligible"] is True and by_session["admin_mode"] is True
    by_key = admin_client.get("/v1/me", headers=_bearer()).get_json()["user"]
    assert "admin_mode" not in by_key


# ---------------------------------------------------------------------------
# Template coverage: ?period=all carries everything profile.html shows
# ---------------------------------------------------------------------------

# Each non-admin field profile.html reads, mapped to its JSON path in
# ``?period=all``. ``[]`` walks every element of a list.
TEMPLATE_FIELDS = {
    "profile_entity.name": "user.name",
    "profile_entity.email": "user.email",
    "profile_entity.active": "user.active",
    "profile_entity.store_conversations": "user.store_conversations",
    "gravatar_url": "user.gravatar_url",
    "profile_groups[].name": "user.groups[]",
    "admin_eligible": "user.admin_eligible",
    # admin_mode is read only when admin_eligible, and is in user.admin_mode on
    # an eligible user's session call: test_admin_mode_is_reported_on_session_calls_only.
    "status.total_tokens_used": "totals.total_tokens",
    "status.total_cost": "totals.cost",
    "model_usage[].model_name": "model_usage[].model_name",
    "model_usage[].requests": "model_usage[].requests",
    "coin_pool.coin_limit": "coin_pool.coin_limit",
    "coin_pool.coins_left": "coin_pool.coins_left",
    "coin_pool.coins_per_hour": "coin_pool.coins_per_hour",
    "coin_pool.next_refill": "coin_pool.next_refill",
    "conversation_count": "lifetime.conversation_count",
    "chat_agg[0]": "lifetime.chat.requests",
    "chat_agg[1]": "lifetime.chat.input_tokens",
    "chat_agg[2]": "lifetime.chat.output_tokens",
    "chat_agg[3]": "lifetime.chat.cost",
    "chat_agg[4]": "lifetime.chat.last_used_at",
    "api_keys[].id": "api_keys[].id",
    "api_keys[].name": "api_keys[].name",
    "api_keys[].key_hint": "api_keys[].key_hint",
    "api_keys[].client_id": "api_keys[].client_id",
    "api_keys[].requested_by": "api_keys[].requested_by",
    "api_keys[].requests": "api_keys[].lifetime.requests",
    "api_keys[].input_tokens": "api_keys[].lifetime.input_tokens",
    "api_keys[].output_tokens": "api_keys[].lifetime.output_tokens",
    "api_keys[].cost": "api_keys[].lifetime.cost",
    "api_keys[].last_used_at": "api_keys[].last_used_at",
    "api_keys[].revoked_at": "api_keys[].revoked_at",
    "api_keys[].expires_at": "api_keys[].expires_at",
    "key_revokers[key.id]": "api_keys[].revoked_by",
    "project_list[].name": "projects[].name",
    "project_list[].detail_url": "projects[].detail_url",
    "project_list[].requests": "projects[].lifetime.requests",
    "project_list[].tokens": "projects[].lifetime.tokens",
    "project_list[].cost": "projects[].lifetime.cost",
    "project_list[].created_at": "projects[].created_at",
    "model_access_list[].model_name": "model_access[].model_name",
    "model_access_list[].model_url": "model_access[].model_url",
    "model_access_list[].access_status": "model_access[].access_status",
    "model_access_list[].consented": "model_access[].consented",
    "model_access_list[].consent_at": "model_access[].consent_at",
    "model_access_list[].model_status": "model_access[].model_status",
    "model_access_list[].notice": "model_access[].notice",
    "model_access_list[].early_notice": "model_access[].early_notice",
    "model_access_list[].last_used_at": "model_access[].lifetime.last_used_at",
    # The access rows' usage figures come from model_usage, joined on model_name.
    "model_access_list[].requests": "model_usage[].requests",
    "model_access_list[].input_tokens": "model_usage[].input_tokens",
    "model_access_list[].output_tokens": "model_usage[].output_tokens",
    "model_access_list[].cost": "model_usage[].cost",
}


def _resolve(node, path):
    """The values at ``path`` (``a.b[].c``); raises KeyError when a step is missing."""
    values = [node]
    for step in path.split("."):
        name, is_list = (step[:-2], True) if step.endswith("[]") else (step, False)
        nxt = []
        for v in values:
            if name not in v:
                raise KeyError(f"{path}: no {name!r}")
            item = v[name]
            if is_list:
                assert isinstance(item, list) and item, f"{path}: {name} is not a non-empty list"
                nxt.extend(item)
            else:
                nxt.append(item)
        values = nxt
    return values


def test_template_fields_map_to_json_paths(seeded, auth_client):
    body = auth_client.get("/v1/me?period=all").get_json()
    for field, path in TEMPLATE_FIELDS.items():
        _resolve(body, path)  # raises when a path is missing
    # Every access row's model has a model_usage row to join.
    assert {r["model_name"] for r in body["model_access"]} <= {u["model_name"] for u in body["model_usage"]}


def test_template_values_match_get_profile_data(app, seeded, auth_client):
    body = auth_client.get("/v1/me?period=all").get_json()
    with app.test_request_context():
        data = _get_profile_data(seeded["eid"], live_tail=True)

    def iso(dt):
        return None if dt is None else dt.isoformat() + "Z"

    assert body["totals"]["total_tokens"] == data["status"]["total_tokens_used"]
    assert body["totals"]["cost"] == pytest.approx(data["status"]["total_cost"])
    assert body["lifetime"]["conversation_count"] == data["conversation_count"]
    chat = data["chat_agg"]
    assert body["lifetime"]["chat"] == {
        "requests": chat[0], "input_tokens": chat[1], "output_tokens": chat[2],
        "cost": pytest.approx(float(chat[3])), "last_used_at": iso(chat[4]),
    }
    assert [(u["model_name"], u["requests"], u["input_tokens"], u["output_tokens"], u["cost"])
            for u in body["model_usage"]] == \
        [(u["model_name"], u["requests"], u["input_tokens"], u["output_tokens"], u["cost"])
         for u in data["model_usage"]]
    assert [(r["model_name"], r["model_url"], r["access_status"], r["consented"], r["model_status"],
             r["lifetime"]["last_used_at"]) for r in body["model_access"]] == \
        [(r["model_name"], r["model_url"], r["access_status"], r["consented"], r["model_status"],
          iso(r["last_used_at"])) for r in data["model_access_list"]]
    pool = data["coin_pool"]
    assert body["coin_pool"] == {"coin_limit": pool["coin_limit"], "coins_left": pool["coins_left"],
                                 "coins_per_hour": pool["coins_per_hour"],
                                 "next_refill": iso(pool["next_refill"])}
    assert [(p["id"], p["name"], p["detail_url"], p["lifetime"]["requests"], p["lifetime"]["tokens"],
             p["lifetime"]["cost"], p["created_at"]) for p in body["projects"]] == \
        [(p["id"], p["name"], p["detail_url"], p["requests"], p["tokens"], p["cost"], iso(p["created_at"]))
         for p in data["project_list"]]
    assert [(k["id"], k["name"], k["key_hint"], k["lifetime"]["requests"], k["lifetime"]["total_tokens"],
             k["lifetime"]["cost"], k["last_used_at"], k["revoked_at"], k["expires_at"], k["revoked_by"])
            for k in body["api_keys"]] == \
        [(str(k.uuid), k.name, k.key_hint, k.requests, k.input_tokens + k.output_tokens, float(k.cost),
          iso(k.last_used_at), iso(k.revoked_at), iso(k.expires_at), data["key_revokers"][k.id])
         for k in data["api_keys"]]


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("period", ["2026-13", "abc", "2026-1", "2026-00", "", "ALL", "0000-01"])
def test_invalid_period_is_400(client, key_id, period):
    _assert_error(client.get(f"/v1/me?period={period}", headers=_bearer()),
                  HTTPStatus.BAD_REQUEST, "invalid_request_error",
                  "Invalid period; expected 'YYYY-MM' (a UTC calendar month) or 'all'")


def test_month_on_sqlite_has_no_usage(seeded, client):
    body = client.get("/v1/me?period=2026-02", headers=_bearer()).get_json()
    assert body["period"] == {"name": "2026-02", "start": "2026-02-01T00:00:00Z",
                              "end": "2026-03-01T00:00:00Z", "usage_available": False}
    assert body["model_usage"] is None and body["totals"] is None
    # Lifetime sections are still there.
    assert body["lifetime"]["chat"]["requests"] == 4


def test_december_ends_on_the_next_new_year(key_id, client):
    period = client.get("/v1/me?period=2025-12", headers=_bearer()).get_json()["period"]
    assert (period["start"], period["end"]) == ("2025-12-01T00:00:00Z", "2026-01-01T00:00:00Z")


def test_default_is_the_current_utc_month(key_id, client):
    now = utcnow()
    period = client.get("/v1/me", headers=_bearer()).get_json()["period"]
    assert period["name"] == f"{now:%Y-%m}"
    assert period["start"] == f"{now:%Y-%m}-01T00:00:00Z"


def test_all_gives_lifetime_figures(seeded, client):
    body = client.get("/v1/me?period=all", headers=_bearer()).get_json()
    assert body["period"] == {"name": "all", "start": None, "end": None, "usage_available": True}
    assert body["totals"] == {"requests": 6, "input_tokens": 110, "output_tokens": 220,
                              "total_tokens": 330, "cost": 0.75}
    [usage] = [u for u in body["model_usage"] if u["model_name"] == seeded["model"]]
    assert (usage["requests"], usage["total_tokens"], usage["status"]) == (6, 330, "ok")
