"""Creating, rotating and editing admin and monitoring keys (profile and project routes).

Only an admin in admin mode can generate or create a non-normal key; the
prefix must match the type; the TTL follows the type's limits (see
lumen/services/key_expiry.py). Project keys are always normal.
"""
from datetime import timedelta
from http import HTTPStatus
from uuid import UUID

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.api_key import KEY_PREFIXES, KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.models.entity_manager import EntityManager
from lumen.services.crypto import hash_api_key
from lumen.services.key_expiry import ADMIN_KEY_MAX_TTL
from lumen.timeutils import utcnow
from tests.conftest import make_project

DAY = 86400
WEEK = 7 * DAY
SECRET = "Q" * 32


def _raw(key_type, secret=SECRET):
    return KEY_PREFIXES[key_type] + secret


def _key(app, kid):
    with app.app_context():
        return db.session.execute(select(APIKey).filter_by(uuid=UUID(kid))).scalar_one()


def _seed(app, entity_id, key_type, raw, expires_at=None):
    with app.app_context():
        key = APIKey(entity_id=entity_id, created_by_entity_id=entity_id, name=f"{key_type}-key",
                     key_hash=hash_api_key(raw), key_hint="x", key_type=key_type, expires_at=expires_at)
        db.session.add(key)
        db.session.commit()
        return str(key.uuid)


def _close_to(expires_at, seconds):
    return abs(expires_at - (utcnow() + timedelta(seconds=seconds))) < timedelta(minutes=1)


# --- generate --------------------------------------------------------------

@pytest.mark.parametrize("key_type", [KEY_TYPE_NORMAL, KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
def test_admin_generates_each_type_with_its_prefix(admin_client, key_type):
    resp = admin_client.get(f"/profile/keys/generate?type={key_type}")
    assert resp.status_code == HTTPStatus.OK
    key = resp.get_json()["key"]
    assert key.startswith(KEY_PREFIXES[key_type])
    if key_type == KEY_TYPE_NORMAL:
        assert not key.startswith(("sk_admin_", "sk_monitor_"))


def test_generate_without_type_gives_normal_key(admin_client):
    key = admin_client.get("/profile/keys/generate").get_json()["key"]
    assert key.startswith("sk_") and not key.startswith(("sk_admin_", "sk_monitor_"))


def test_generate_normal_key_never_uses_a_reserved_prefix(admin_client, monkeypatch):
    tokens = iter(["admin_" + "a" * 37, "monitor_" + "b" * 35, "c" * 43])
    monkeypatch.setattr("lumen.blueprints.profile.routes.secrets.token_urlsafe", lambda n: next(tokens))
    assert admin_client.get("/profile/keys/generate").get_json()["key"] == "sk_" + "c" * 43


@pytest.mark.parametrize("client_fixture", ["auth_client", "admin_client_no_mode"])
@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
def test_generate_non_normal_requires_admin_mode(request, client_fixture, key_type):
    client = request.getfixturevalue(client_fixture)
    resp = client.get(f"/profile/keys/generate?type={key_type}")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "error" in resp.get_json()


def test_non_admin_can_still_generate_normal(auth_client):
    assert auth_client.get("/profile/keys/generate?type=normal").status_code == HTTPStatus.OK


@pytest.mark.parametrize("client_fixture", ["auth_client", "admin_client"])
def test_generate_unknown_type_returns_400(request, client_fixture):
    client = request.getfixturevalue(client_fixture)
    assert client.get("/profile/keys/generate?type=root").status_code == HTTPStatus.BAD_REQUEST


# --- create ----------------------------------------------------------------

@pytest.mark.parametrize("key_type, hint", [
    (KEY_TYPE_NORMAL, "sk_QQQQ...QQQQ"),
    (KEY_TYPE_ADMIN, "sk_admin_QQQQ...QQQQ"),
    (KEY_TYPE_MONITORING, "sk_monitor_QQQQ...QQQQ"),
])
def test_admin_creates_each_type(app, admin_client, key_type, hint):
    resp = admin_client.post("/profile/keys", json={"name": "k", "key": _raw(key_type), "type": key_type})
    assert resp.status_code == HTTPStatus.CREATED
    key = _key(app, resp.get_json()["id"])
    assert key.key_type == key_type
    assert key.key_hint == hint
    assert key.key_hash == hash_api_key(_raw(key_type))


def test_create_without_type_gives_normal_key(app, admin_client):
    resp = admin_client.post("/profile/keys", json={"name": "k", "key": _raw(KEY_TYPE_NORMAL)})
    assert resp.status_code == HTTPStatus.CREATED
    assert _key(app, resp.get_json()["id"]).key_type == KEY_TYPE_NORMAL


@pytest.mark.parametrize("client_fixture", ["auth_client", "admin_client_no_mode"])
@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
def test_create_non_normal_requires_admin_mode(app, request, client_fixture, key_type):
    client = request.getfixturevalue(client_fixture)
    resp = client.post("/profile/keys", json={"name": "nope", "key": _raw(key_type), "type": key_type})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    with app.app_context():
        assert db.session.execute(select(APIKey).filter_by(name="nope")).first() is None


@pytest.mark.parametrize("key_type", ["root", "Admin", 1, ["admin"]])
def test_create_unknown_type_returns_400(admin_client, key_type):
    resp = admin_client.post("/profile/keys", json={"name": "k", "key": _raw(KEY_TYPE_NORMAL), "type": key_type})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"] == "Invalid key type"


@pytest.mark.parametrize("key_type, raw", [
    (KEY_TYPE_NORMAL, _raw(KEY_TYPE_ADMIN)),        # a normal key may not look like an admin key
    (KEY_TYPE_NORMAL, _raw(KEY_TYPE_MONITORING)),
    (KEY_TYPE_ADMIN, _raw(KEY_TYPE_NORMAL)),
    (KEY_TYPE_ADMIN, _raw(KEY_TYPE_MONITORING)),
    (KEY_TYPE_MONITORING, _raw(KEY_TYPE_ADMIN)),
])
def test_create_prefix_type_mismatch_returns_400(app, admin_client, key_type, raw):
    resp = admin_client.post("/profile/keys", json={"name": "mismatch", "key": raw, "type": key_type})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "prefix" in resp.get_json()["error"]
    with app.app_context():
        assert db.session.execute(select(APIKey).filter_by(name="mismatch")).first() is None


# --- TTL on create ---------------------------------------------------------

@pytest.mark.parametrize("cap, expected", [(0, WEEK), (30 * DAY, WEEK), (DAY, DAY)])
def test_admin_key_default_ttl_is_longest_allowed(app, admin_client, restore_config, cap, expected):
    app.config["KEY_TTL"] = cap
    resp = admin_client.post("/profile/keys", json={"key": _raw(KEY_TYPE_ADMIN), "type": KEY_TYPE_ADMIN})
    assert resp.status_code == HTTPStatus.CREATED
    assert _close_to(_key(app, resp.get_json()["id"]).expires_at, expected)


@pytest.mark.parametrize("cap, ttl", [(0, WEEK + 1), (0, 0), (30 * DAY, 30 * DAY), (DAY, DAY + 1), (DAY, 0)])
def test_admin_key_ttl_over_limit_or_never_returns_400(app, admin_client, restore_config, cap, ttl):
    app.config["KEY_TTL"] = cap
    resp = admin_client.post("/profile/keys", json={"key": _raw(KEY_TYPE_ADMIN), "type": KEY_TYPE_ADMIN, "ttl": ttl})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "ttl" in resp.get_json()["error"]


def test_admin_key_accepts_ttl_within_a_week(app, admin_client, restore_config):
    app.config["KEY_TTL"] = 0
    resp = admin_client.post("/profile/keys", json={"key": _raw(KEY_TYPE_ADMIN), "type": KEY_TYPE_ADMIN, "ttl": DAY})
    assert resp.status_code == HTTPStatus.CREATED
    assert _close_to(_key(app, resp.get_json()["id"]).expires_at, DAY)


@pytest.mark.parametrize("cap", [0, DAY])
def test_monitoring_key_defaults_to_never(app, admin_client, restore_config, cap):
    app.config["KEY_TTL"] = cap
    resp = admin_client.post("/profile/keys", json={"key": _raw(KEY_TYPE_MONITORING), "type": KEY_TYPE_MONITORING})
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.get_json()["expires_at"] is None
    assert _key(app, resp.get_json()["id"]).expires_at is None


@pytest.mark.parametrize("ttl", [0, 365 * DAY])
def test_monitoring_key_ignores_the_cap(app, admin_client, restore_config, ttl):
    app.config["KEY_TTL"] = DAY
    resp = admin_client.post("/profile/keys",
                             json={"key": _raw(KEY_TYPE_MONITORING), "type": KEY_TYPE_MONITORING, "ttl": ttl})
    assert resp.status_code == HTTPStatus.CREATED
    expires_at = _key(app, resp.get_json()["id"]).expires_at
    assert expires_at is None if ttl == 0 else _close_to(expires_at, ttl)


# --- TTL on edit -----------------------------------------------------------

@pytest.mark.parametrize("cap, ttl", [(0, WEEK + 1), (0, 0), (DAY, WEEK)])
def test_edit_admin_key_ttl_over_limit_returns_400(app, admin_client, admin_user, restore_config, cap, ttl):
    app.config["KEY_TTL"] = cap
    kid = _seed(app, admin_user["id"], KEY_TYPE_ADMIN, _raw(KEY_TYPE_ADMIN), expires_at=utcnow() + timedelta(days=1))
    resp = admin_client.post(f"/profile/keys/{kid}/ttl", json={"ttl": ttl})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "ttl" in resp.get_json()["error"]


def test_edit_admin_key_ttl_within_a_week(app, admin_client, admin_user, restore_config):
    app.config["KEY_TTL"] = 0
    kid = _seed(app, admin_user["id"], KEY_TYPE_ADMIN, _raw(KEY_TYPE_ADMIN), expires_at=utcnow() + timedelta(days=1))
    resp = admin_client.post(f"/profile/keys/{kid}/ttl", json={"ttl": ADMIN_KEY_MAX_TTL})
    assert resp.status_code == HTTPStatus.OK
    assert _close_to(_key(app, kid).expires_at, WEEK)


@pytest.mark.parametrize("ttl", [0, 365 * DAY])
def test_edit_monitoring_key_ttl_ignores_the_cap(app, admin_client, admin_user, restore_config, ttl):
    app.config["KEY_TTL"] = DAY
    kid = _seed(app, admin_user["id"], KEY_TYPE_MONITORING, _raw(KEY_TYPE_MONITORING))
    resp = admin_client.post(f"/profile/keys/{kid}/ttl", json={"ttl": ttl})
    assert resp.status_code == HTTPStatus.OK
    expires_at = _key(app, kid).expires_at
    assert expires_at is None if ttl == 0 else _close_to(expires_at, ttl)


# --- rotate ----------------------------------------------------------------

@pytest.mark.parametrize("key_type, hint", [
    (KEY_TYPE_ADMIN, "sk_admin_RRRR...RRRR"),
    (KEY_TYPE_MONITORING, "sk_monitor_RRRR...RRRR"),
])
def test_rotate_keeps_type_and_prefix_hint(app, admin_client, admin_user, key_type, hint):
    kid = _seed(app, admin_user["id"], key_type, _raw(key_type))
    new = _raw(key_type, "R" * 32)
    resp = admin_client.post(f"/profile/keys/{kid}/rotate", json={"key": new})
    assert resp.status_code == HTTPStatus.OK
    key = _key(app, kid)
    assert key.key_type == key_type
    assert key.key_hint == hint
    assert key.key_hash == hash_api_key(new)


@pytest.mark.parametrize("key_type, new_type", [
    (KEY_TYPE_ADMIN, KEY_TYPE_NORMAL),
    (KEY_TYPE_ADMIN, KEY_TYPE_MONITORING),
    (KEY_TYPE_MONITORING, KEY_TYPE_ADMIN),
    (KEY_TYPE_NORMAL, KEY_TYPE_ADMIN),
    (KEY_TYPE_NORMAL, KEY_TYPE_MONITORING),
])
def test_rotate_refuses_mismatched_prefix(app, admin_client, admin_user, key_type, new_type):
    kid = _seed(app, admin_user["id"], key_type, _raw(key_type))
    resp = admin_client.post(f"/profile/keys/{kid}/rotate", json={"key": _raw(new_type, "R" * 32)})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert _key(app, kid).key_hash == hash_api_key(_raw(key_type))


# --- project keys ----------------------------------------------------------

@pytest.fixture
def service_project(app, test_user):
    """A project owned and managed by test_user."""
    with app.app_context():
        project = make_project("key-type-svc", initials="KT")
        project.owner_entity_id = test_user["id"]
        db.session.add(EntityManager(user_entity_id=test_user["id"], project_entity_id=project.id))
        db.session.commit()
        return {"id": project.id, "uuid": str(project.uuid)}


@pytest.fixture
def owned_project_client(auth_client, service_project):
    return auth_client


@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, "root"])
def test_project_key_create_refuses_non_normal_type(app, owned_project_client, service_project, key_type):
    resp = owned_project_client.post(f"/projects/{service_project['uuid']}/keys",
                                     json={"name": "proj", "key": _raw(KEY_TYPE_NORMAL), "type": key_type})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    with app.app_context():
        assert db.session.execute(select(APIKey).filter_by(name="proj")).first() is None


@pytest.mark.parametrize("body_type", [{}, {"type": KEY_TYPE_NORMAL}])
def test_project_key_create_normal(app, owned_project_client, service_project, body_type):
    resp = owned_project_client.post(f"/projects/{service_project['uuid']}/keys",
                                     json={"name": "proj", "key": _raw(KEY_TYPE_NORMAL), **body_type})
    assert resp.status_code == HTTPStatus.CREATED
    assert _key(app, resp.get_json()["id"]).key_type == KEY_TYPE_NORMAL


def test_project_key_create_refuses_reserved_prefix(owned_project_client, service_project):
    resp = owned_project_client.post(f"/projects/{service_project['uuid']}/keys",
                                     json={"name": "proj", "key": _raw(KEY_TYPE_ADMIN)})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_project_rotate_refuses_reserved_prefix(app, owned_project_client, service_project, test_user):
    with app.app_context():
        key = APIKey(entity_id=service_project["id"], created_by_entity_id=test_user["id"], name="proj",
                     key_hash=hash_api_key(_raw(KEY_TYPE_NORMAL)), key_hint="x")
        db.session.add(key)
        db.session.commit()
        kid = str(key.uuid)
    resp = owned_project_client.post(f"/projects/{service_project['uuid']}/keys/{kid}/rotate",
                                     json={"key": _raw(KEY_TYPE_MONITORING, "R" * 32)})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert _key(app, kid).key_hash == hash_api_key(_raw(KEY_TYPE_NORMAL))


# --- profile page ----------------------------------------------------------

def _ttl_sets(soup):
    return {t["data-key-type"]: [(o["value"], o.has_attr("selected")) for o in t.find_all("option")]
            for t in soup.select("template.key-ttl-options")}


def test_type_select_renders_in_admin_mode(admin_client):
    soup = BeautifulSoup(admin_client.get("/profile").data, "html.parser")
    select_el = soup.find("select", id="key-type-select")
    assert [o["value"] for o in select_el.find_all("option")] == ["normal", "admin", "monitoring"]
    assert select_el.find("option", selected=True)["value"] == "normal"
    assert soup.find("label", attrs={"for": "key-type-select"}).get_text(strip=True) == "Key type"
    assert soup.find(id=select_el["aria-describedby"]) is not None


@pytest.mark.parametrize("client_fixture", ["auth_client", "admin_client_no_mode"])
def test_type_select_hidden_outside_admin_mode(request, client_fixture):
    client = request.getfixturevalue(client_fixture)
    soup = BeautifulSoup(client.get("/profile").data, "html.parser")
    assert soup.find(id="key-type-select") is None
    # The expiry option sets are still there, for editing an existing key of any type.
    assert len(soup.select("template.key-ttl-options")) == 3


def test_profile_renders_all_three_expiry_option_sets(app, admin_client, restore_config):
    app.config["KEY_TTL"] = 30 * DAY
    sets = _ttl_sets(BeautifulSoup(admin_client.get("/profile").data, "html.parser"))
    assert set(sets) == {"normal", "admin", "monitoring"}
    assert sets["normal"][-1] == (str(30 * DAY), True)
    assert sets["admin"] == [(str(DAY), False), (str(WEEK), True)]
    assert sets["monitoring"][-1] == ("0", True)


def test_profile_key_rows_carry_type(app, admin_client, admin_user):
    _seed(app, admin_user["id"], KEY_TYPE_MONITORING, _raw(KEY_TYPE_MONITORING))
    html = admin_client.get("/profile").get_data(as_text=True)
    assert 'type: "monitoring"' in html
    assert "KEY_TYPE_LABELS" in html


def test_admin_view_of_user_profile_carries_key_type(app, admin_client, test_user):
    _seed(app, test_user["id"], KEY_TYPE_ADMIN, _raw(KEY_TYPE_ADMIN))
    html = admin_client.get(f"/admin/users/{test_user['uuid']}/profile").get_data(as_text=True)
    assert 'type: "admin"' in html


@pytest.mark.parametrize("client_fixture", ["auth_client", "admin_client_no_mode"])
@pytest.mark.parametrize("key_type", [KEY_TYPE_ADMIN, KEY_TYPE_MONITORING])
def test_rotate_non_normal_requires_admin_mode(app, request, client_fixture, key_type, test_user, admin_user):
    client = request.getfixturevalue(client_fixture)
    owner = test_user if client_fixture == "auth_client" else admin_user
    kid = _seed(app, owner["id"], key_type, _raw(key_type))
    resp = client.post(f"/profile/keys/{kid}/rotate", json={"key": _raw(key_type, "R" * 32)})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert _key(app, kid).key_hash == hash_api_key(_raw(key_type))


def test_rotate_normal_key_needs_no_admin_mode(app, admin_client_no_mode, admin_user):
    kid = _seed(app, admin_user["id"], KEY_TYPE_NORMAL, _raw(KEY_TYPE_NORMAL))
    resp = admin_client_no_mode.post(f"/profile/keys/{kid}/rotate", json={"key": _raw(KEY_TYPE_NORMAL, "R" * 32)})
    assert resp.status_code == HTTPStatus.OK
