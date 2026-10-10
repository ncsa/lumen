"""Tests for the profile blueprint routes."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from http import HTTPStatus

import pytest

from lumen.timeutils import utcnow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model_endpoint(app, model_id, healthy=True):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        ep = ModelEndpoint(
            model_config_id=model_id,
            url="http://localhost:9999/v1",
            api_key="k",
            healthy=healthy,
        )
        db.session.add(ep)
        db.session.commit()


# ---------------------------------------------------------------------------
# Profile index page
# ---------------------------------------------------------------------------

def test_profile_page_requires_login(client):
    resp = client.get("/profile", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_profile_page_with_model(app, auth_client, test_model):
    _make_model_endpoint(app, test_model["id"])
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK


def test_profile_page_with_degraded_model(app, auth_client, test_model):
    """Two endpoints; one unhealthy → degraded status."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(model_config_id=test_model["id"], url="http://a/v1", api_key="k", healthy=True))
        db.session.add(ModelEndpoint(model_config_id=test_model["id"], url="http://b/v1", api_key="k", healthy=False))
        db.session.commit()
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK


def test_profile_page_with_down_model(app, auth_client, test_model):
    """Endpoint present but unhealthy → down status."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(model_config_id=test_model["id"], url="http://a/v1", api_key="k", healthy=False))
        db.session.commit()
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK


def test_profile_page_with_no_endpoints(app, auth_client, test_model):
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# project_profile_page redirect
# ---------------------------------------------------------------------------

def test_project_profile_page_redirects(app, auth_client):
    with app.app_context():
        from tests.conftest import make_project
        sid = make_project("svc", initials="SV").id

    resp = auth_client.get(f"/profile/project/{sid}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.MOVED_PERMANENTLY
    assert "/projects/" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# generate_key
# ---------------------------------------------------------------------------

def test_generate_key_returns_key(auth_client):
    resp = auth_client.get("/profile/keys/generate")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["key"].startswith("sk_")


def test_generate_key_requires_login(client):
    resp = client.get("/profile/keys/generate", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


# ---------------------------------------------------------------------------
# create_key
# ---------------------------------------------------------------------------

def test_create_key_success(auth_client):
    key = "sk_" + "a" * 32
    resp = auth_client.post(
        "/profile/keys",
        data=json.dumps({"name": "my key", "key": key}),
        content_type="application/json",
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.get_json()
    assert data["name"] == "my key"
    assert "id" in data


def test_create_key_default_name(auth_client):
    key = "sk_" + "b" * 32
    resp = auth_client.post(
        "/profile/keys",
        data=json.dumps({"key": key}),
        content_type="application/json",
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.get_json()["name"] == "Unnamed Key"


def test_create_key_records_creator(app, auth_client, test_user):
    key = "sk_" + "c" * 32
    resp = auth_client.post(
        "/profile/keys",
        data=json.dumps({"key": key}),
        content_type="application/json",
    )
    assert resp.status_code == HTTPStatus.CREATED
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        api_key = db.session.execute(select(APIKey).filter_by(key_hash=hash_api_key(key))).scalar_one()
        assert api_key.created_by_entity_id == test_user["id"]


def test_create_key_invalid_key(auth_client):
    resp = auth_client.post(
        "/profile/keys",
        data=json.dumps({"key": "badkey"}),
        content_type="application/json",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_key_missing_key(auth_client):
    resp = auth_client.post(
        "/profile/keys",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_key_duplicate(auth_client):
    key = "sk_" + "c" * 32
    auth_client.post("/profile/keys", data=json.dumps({"key": key}), content_type="application/json")
    resp = auth_client.post("/profile/keys", data=json.dumps({"key": key}), content_type="application/json")
    assert resp.status_code == HTTPStatus.CONFLICT


def test_create_key_requires_login(client):
    resp = client.post("/profile/keys", data=json.dumps({"key": "sk_x"}), content_type="application/json", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


# ---------------------------------------------------------------------------
# delete_key
# ---------------------------------------------------------------------------

def test_delete_key_success(app, auth_client, test_user):
    key = "sk_" + "d" * 32
    create_resp = auth_client.post(
        "/profile/keys",
        data=json.dumps({"name": "to delete", "key": key}),
        content_type="application/json",
    )
    kid = create_resp.get_json()["id"]

    resp = auth_client.delete(f"/profile/keys/{kid}")
    assert resp.status_code == HTTPStatus.NO_CONTENT


def _make_personal_key(app, entity_id, raw, revoked_at=None):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        ak = APIKey(
            entity_id=entity_id, created_by_entity_id=entity_id, name="usage key",
            key_hash=hash_api_key(raw), key_hint=f"{raw[:8]}...{raw[-4:]}", revoked_at=revoked_at,
            requests=9, input_tokens=300, output_tokens=120, audio_seconds=3, cost=Decimal("2.250000"),
        )
        db.session.add(ak)
        db.session.commit()
        return ak.id


def _personal_key_state(app, kid):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        k = db.session.get(APIKey, kid)
        assert k is not None
        return k.revoked_at, k.requests, k.input_tokens, k.output_tokens, k.audio_seconds, k.cost


def test_delete_key_soft_deletes(app, auth_client, test_user):
    kid = _make_personal_key(app, test_user["id"], "sk_" + "s" * 32)
    resp = auth_client.delete(f"/profile/keys/{kid}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    revoked_at, *counters = _personal_key_state(app, kid)
    assert abs(utcnow() - revoked_at) < timedelta(minutes=1)
    assert counters == [9, 300, 120, 3, Decimal("2.250000")]


def test_delete_inactive_key_is_noop(app, auth_client, test_user):
    kid = _make_personal_key(app, test_user["id"], "sk_" + "i" * 32, revoked_at=datetime(2026, 9, 2, 8, 0))
    before = _personal_key_state(app, kid)
    resp = auth_client.delete(f"/profile/keys/{kid}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert _personal_key_state(app, kid) == before


def test_deleted_key_is_rejected(app, client, auth_client, test_user):
    raw = "sk_" + "z" * 32
    kid = _make_personal_key(app, test_user["id"], raw)
    headers = {"Authorization": f"Bearer {raw}"}
    assert client.get("/v1/models", headers=headers).status_code == HTTPStatus.OK
    assert auth_client.delete(f"/profile/keys/{kid}").status_code == HTTPStatus.NO_CONTENT
    assert client.get("/v1/models", headers=headers).status_code == HTTPStatus.UNAUTHORIZED


def test_delete_key_forbidden(app, auth_client, admin_user):
    """Key owned by admin_user cannot be deleted by auth_client (test_user)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        raw = "sk_" + "e" * 32
        ak = APIKey(
            entity_id=admin_user["id"],
            name="admin key",
            key_hash=hash_api_key(raw),
            key_hint="sk_eeeee...eeee",
        )
        db.session.add(ak)
        db.session.commit()
        db.session.refresh(ak)
        kid = ak.id

    resp = auth_client.delete(f"/profile/keys/{kid}")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_delete_key_not_found(auth_client):
    resp = auth_client.delete("/profile/keys/999999")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_delete_key_requires_login(client):
    resp = client.delete("/profile/keys/1", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


# ---------------------------------------------------------------------------
# rotate_key
# ---------------------------------------------------------------------------

def _seed_key_with_usage(app, entity_id, raw, revoked_at=None):
    """Insert a key with non-zero usage counters; return its id."""
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.services.crypto import hash_api_key
    with app.app_context():
        ak = APIKey(
            entity_id=entity_id, created_by_entity_id=entity_id, name="rotating",
            key_hash=hash_api_key(raw), key_hint=f"{raw[:7]}...{raw[-4:]}", revoked_at=revoked_at,
            requests=5, input_tokens=100, output_tokens=40, audio_seconds=7,
            cost=Decimal("1.500000"), last_used_at=datetime(2026, 9, 1, 12, 0),
            created_at=datetime(2026, 8, 1, 9, 0),
        )
        db.session.add(ak)
        db.session.commit()
        return ak.id


def _key_snapshot(app, kid):
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    with app.app_context():
        ak = db.session.get(APIKey, kid)
        return {c: getattr(ak, c) for c in (
            "requests", "input_tokens", "output_tokens", "audio_seconds", "cost",
            "last_used_at", "name", "created_at", "created_by_entity_id", "entity_id", "revoked_at",
            "key_hash", "key_hint",
        )}


def test_rotate_key_swaps_secret_and_keeps_stats(app, client, auth_client, test_user):
    old, new = "sk_" + "r" * 32, "sk_" + "n" * 32
    kid = _seed_key_with_usage(app, test_user["id"], old)
    before = _key_snapshot(app, kid)

    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": new})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == {"id": kid, "name": "rotating", "key": new}

    after = _key_snapshot(app, kid)
    old_hash, new_hash = before.pop("key_hash"), after.pop("key_hash")
    new_hint = after.pop("key_hint")
    before.pop("key_hint")
    assert new_hash != old_hash
    assert new_hint == f"{new[:7]}...{new[-4:]}"
    assert after == before

    assert client.get("/v1/usage", headers={"Authorization": f"Bearer {old}"}).status_code == HTTPStatus.UNAUTHORIZED
    usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {new}"})
    assert usage.status_code == HTTPStatus.OK
    assert usage.get_json()["requests"] == 5


def test_rotate_key_forbidden_for_other_user(app, auth_client, admin_user):
    kid = _seed_key_with_usage(app, admin_user["id"], "sk_" + "o" * 32)
    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": "sk_" + "p" * 32})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_rotate_key_inactive_returns_409(app, auth_client, test_user):
    kid = _seed_key_with_usage(app, test_user["id"], "sk_" + "i" * 32, revoked_at=datetime(2026, 9, 2, 8, 0))
    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": "sk_" + "j" * 32})
    assert resp.status_code == HTTPStatus.CONFLICT


@pytest.mark.parametrize("payload", [
    {}, {"key": ""}, {"key": "badkey"}, {"key": 123}, {"key": ["sk_" + "v" * 32]}, ["sk_" + "v" * 32],
])
def test_rotate_key_invalid_key_returns_400(app, auth_client, test_user, payload):
    kid = _seed_key_with_usage(app, test_user["id"], "sk_" + "v" * 32)
    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json=payload)
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_rotate_key_duplicate_returns_409(app, auth_client, test_user):
    kid = _seed_key_with_usage(app, test_user["id"], "sk_" + "w" * 32)
    taken = "sk_" + "x" * 32
    auth_client.post("/profile/keys", json={"key": taken})
    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": taken})
    assert resp.status_code == HTTPStatus.CONFLICT
    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": "sk_" + "w" * 32})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_rotate_key_concurrent_duplicate_returns_409(app, auth_client, test_user, monkeypatch):
    """A secret committed by another request after the duplicate check yields 409, not 500."""
    from lumen.blueprints.profile import routes as profile_routes
    from lumen.services.crypto import hash_api_key
    old, raced = "sk_" + "q" * 32, "sk_" + "k" * 32
    kid = _seed_key_with_usage(app, test_user["id"], old)
    auth_client.post("/profile/keys", json={"key": raced})
    # Skip the pre-check so the unique constraint on key_hash is what catches the duplicate.
    monkeypatch.setattr(profile_routes, "_new_key_fields",
                        lambda key: ({"key_hash": hash_api_key(key), "key_hint": "sk_kkkk...kkkk"}, None))

    resp = auth_client.post(f"/profile/keys/{kid}/rotate", json={"key": raced})
    assert resp.status_code == HTTPStatus.CONFLICT
    assert _key_snapshot(app, kid)["key_hint"] == f"{old[:7]}...{old[-4:]}"


def test_rotate_key_not_found(auth_client):
    resp = auth_client.post("/profile/keys/999999/rotate", json={"key": "sk_" + "y" * 32})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_rotate_key_requires_login(client):
    resp = client.post("/profile/keys/1/rotate", json={"key": "sk_" + "z" * 32}, follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


# ---------------------------------------------------------------------------
# user_consent
# ---------------------------------------------------------------------------

def _make_needs_ack_model(app, entity_id, model_name="ack-model"):
    """Create a public needs_ack model (visible to the entity)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(
            model_name=model_name,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            needs_ack=True,
        )
        db.session.add(mc)
        db.session.commit()
        db.session.refresh(mc)
        return {"id": mc.id, "model_name": mc.model_name}


def test_user_consent_requires_login(client):
    resp = client.post("/profile/consent/some-model", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_user_consent_model_not_found(auth_client):
    resp = auth_client.post("/profile/consent/nonexistent-model-xyz")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_user_consent_no_ack_required(app, auth_client, test_model):
    """Posting consent for a model with no acknowledgement requirement returns 400."""
    resp = auth_client.post(f"/profile/consent/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_user_consent_blocked_model_matches_not_found(app, auth_client, test_model):
    from tests.conftest import set_model_owner

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity

        owner = Entity(
            entity_type="user", email="model-owner@example.com", name="Model Owner", active=True
        )
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)

    blocked = auth_client.post(f"/profile/consent/{test_model['model_name']}")
    unknown = auth_client.post("/profile/consent/nonexistent-model-xyz")
    assert blocked.status_code == unknown.status_code == HTTPStatus.NOT_FOUND
    assert blocked.data == unknown.data


def test_user_consent_success(app, auth_client, test_user):
    """Posting consent for a needs_ack model records it and returns 200."""
    gm = _make_needs_ack_model(app, test_user["id"])
    resp = auth_client.post(f"/profile/consent/{gm['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["ok"] is True

    # Verify consent was persisted
    with app.app_context():
        from lumen.services.llm import has_model_consent
        assert has_model_consent(test_user["id"], gm["id"])


def test_user_consent_idempotent(app, auth_client, test_user):
    """Posting consent twice is idempotent — second call still returns 200."""
    gm = _make_needs_ack_model(app, test_user["id"], model_name="ack-model-2")
    auth_client.post(f"/profile/consent/{gm['model_name']}")
    resp = auth_client.post(f"/profile/consent/{gm['model_name']}")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Hidden model metadata
# ---------------------------------------------------------------------------

def test_profile_page_hides_inactive_model(app, auth_client):
    """Inactive model metadata is absent from the profile page."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(
            model_name="inactive-model",
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            disabled=True,
        )
        db.session.add(mc)
        db.session.commit()

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    assert b"inactive-model" not in resp.data


# ---------------------------------------------------------------------------
# Coin pool (EntityLimit with positive balance)
# ---------------------------------------------------------------------------

def test_profile_page_with_coin_pool(app, auth_client, test_user):
    """Profile page renders correctly when entity has a token limit (coin pool)."""
    with app.app_context():
        from datetime import datetime, timezone

        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"],
            max_coins=100,
            refresh_coins=10,
            starting_coins=50,
        ))
        db.session.add(EntityBalance(
            entity_id=test_user["id"],
            coins_left=45,
            last_refill_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Hidden models with past usage
# ---------------------------------------------------------------------------

def test_profile_page_hides_inactive_model_with_past_usage(app, auth_client, test_user):
    """Past usage does not expose metadata for a model that is now inactive."""
    with app.app_context():
        from datetime import datetime, timezone

        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.models.model_stat import ModelStat
        mc = ModelConfig(
            model_name="retired-model",
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            disabled=True,
        )
        db.session.add(mc)
        db.session.flush()
        db.session.add(ModelStat(
            entity_id=test_user["id"],
            model_config_id=mc.id,
            source="api",
            requests=5,
            input_tokens=100,
            output_tokens=200,
            cost=0.01,
            last_used_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    assert b"retired-model" not in resp.data


def test_profile_page_hides_blocked_model(app, auth_client, test_model):
    from tests.conftest import set_model_owner

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity

        owner = Entity(entity_type="user", email="private-owner@example.com", name="Private Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_profile_page_shows_needs_ack_model(app, auth_client, test_user):
    model = _make_needs_ack_model(app, test_user["id"], model_name="profile-needs-ack")

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    assert model["model_name"].encode() in resp.data


# ---------------------------------------------------------------------------
# Projects (clients) section
# ---------------------------------------------------------------------------

def _make_managed_project(app, user_id, name="managed-svc"):
    """Create an active project entity owned (and so managed) by user_id; return its id."""
    with app.app_context():
        from tests.conftest import make_project
        return make_project(name, owner_id=user_id, initials="MS").id


def test_profile_projects_section_hidden_when_none(auth_client):
    """No managed projects → the Projects section is not rendered."""
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="project-table"' not in body
    assert 'id="project-search"' not in body


def test_profile_projects_section_shows_managed_project(app, auth_client, test_user):
    """A managed project appears in the Projects section of the user's profile."""
    _make_managed_project(app, test_user["id"], name="my-client-svc")
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    # Section scaffolding present
    assert 'id="project-table"' in body
    assert 'id="project-search"' in body
    # Project name is emitted into the JS rows
    assert "my-client-svc" in body


def test_profile_projects_section_includes_inactive(app, auth_client, test_user):
    """An inactive managed project is still shown so the manager can reach and re-enable it."""
    with app.app_context():
        from tests.conftest import make_project
        make_project("inactive-client", owner_id=test_user["id"], initials="IC", active=False)

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="project-table"' in body
    assert "inactive-client" in body


def test_profile_key_table_has_rotate_button(auth_client):
    html = auth_client.get("/profile").get_data(as_text=True)
    assert 'class="btn btn-sm btn-outline-secondary rotate-key-btn"' in html
    assert 'aria-label="Rotate key ${escHtml(k.name)}"' in html
    assert 'id="rotatedKeyModal"' in html
    assert "js/key-rotate.js" in html


def test_admin_user_profile_has_no_rotate(admin_client, test_user):
    """The admin view of another user's profile stays read-only: no rotate flow is loaded."""
    resp = admin_client.get(f"/admin/users/{test_user['id']}/profile")
    assert resp.status_code == HTTPStatus.OK
    html = resp.get_data(as_text=True)
    assert 'id="rotatedKeyModal"' not in html
    assert "js/key-rotate.js" not in html


def test_admin_user_profile_shows_projects_section(app, admin_client, test_user):
    """Admin viewing another user's profile also sees that user's Projects section."""
    _make_managed_project(app, test_user["id"], name="user-client")
    resp = admin_client.get(f"/admin/users/{test_user['id']}/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="project-table"' in body
    assert "user-client" in body


def test_profile_projects_zero_usage_renders_zero_not_dash(app, auth_client, test_user):
    """A project with no usage shows 0, not — (matches the Keys table behavior)."""
    _make_managed_project(app, test_user["id"], name="zero-usage-svc")
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    # The JS row data for a zero-usage project must carry 0, not null/undefined.
    assert "zero-usage-svc" in body
    assert "requests: 0" in body
    assert "tokens: 0" in body
    # The falsy-zero ternary that rendered 0 as — must be absent.
    assert "p.requests ?" not in body
    assert "p.tokens ?" not in body


# ---------------------------------------------------------------------------
# profile tabs
# ---------------------------------------------------------------------------


def test_profile_page_renders_tabs(auth_client):
    """Profile sections are grouped into tabs; Projects tab absent without projects."""
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="profile-tabs"' in body
    assert 'id="tab-chat"' in body
    assert 'id="tab-models"' in body
    assert 'id="tab-projects"' not in body
    assert 'id="pane-chat"' in body
    assert 'id="pane-models"' in body


def test_profile_page_projects_tab_with_project(app, auth_client, test_user):
    """A managed project adds the Projects tab and its pane."""
    _make_managed_project(app, test_user["id"], name="tabbed-client")
    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="tab-projects"' in body
    assert 'id="pane-projects"' in body


def test_admin_user_profile_renders_tabs(admin_client, test_user):
    """Admin read-only profile view uses the same tab layout."""
    resp = admin_client.get(f"/admin/users/{test_user['id']}/profile")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="profile-tabs"' in body
    assert 'id="tab-chat"' in body
    assert 'id="tab-models"' in body


# ---------------------------------------------------------------------------
# set_admin_mode / admin mode gating
# ---------------------------------------------------------------------------

def test_admin_mode_off_by_default(app, admin_client_no_mode):
    """Eligible admin without admin mode is a normal user: no /admin access, no admin nav."""
    resp = admin_client_no_mode.get("/admin/users")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    page = admin_client_no_mode.get("/profile")
    assert page.status_code == HTTPStatus.OK
    assert "/admin/users" not in page.get_data(as_text=True)


def test_admin_mode_toggle_grants_and_revokes_access(app, admin_client_no_mode):
    # Prime the session _nav cache with is_admin False.
    admin_client_no_mode.get("/profile")

    resp = admin_client_no_mode.post("/profile/settings/admin-mode", json={"enabled": True})
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["admin_mode"] is True

    assert admin_client_no_mode.get("/admin/users").status_code == HTTPStatus.OK
    page = admin_client_no_mode.get("/profile")
    assert "/admin/users" in page.get_data(as_text=True)

    resp = admin_client_no_mode.post("/profile/settings/admin-mode", json={"enabled": False})
    assert resp.status_code == HTTPStatus.OK
    assert admin_client_no_mode.get("/admin/users").status_code == HTTPStatus.FORBIDDEN
    page = admin_client_no_mode.get("/profile")
    assert "/admin/users" not in page.get_data(as_text=True)


def test_admin_mode_forbidden_for_non_admin(auth_client):
    resp = auth_client.post("/profile/settings/admin-mode", json={"enabled": True})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert auth_client.get("/admin/users").status_code == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("payload", [{}, {"enabled": "yes"}, {"enabled": 1}])
def test_admin_mode_bad_payload(admin_client_no_mode, payload):
    resp = admin_client_no_mode.post("/profile/settings/admin-mode", json=payload)
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_admin_mode_requires_login(client):
    resp = client.post("/profile/settings/admin-mode", json={"enabled": True}, follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_admin_mode_toggle_shown_for_eligible_admin(admin_client_no_mode):
    body = admin_client_no_mode.get("/profile").get_data(as_text=True)
    assert 'id="admin-mode-toggle"' in body


def test_admin_mode_toggle_hidden_for_regular_user(auth_client):
    body = auth_client.get("/profile").get_data(as_text=True)
    assert 'id="admin-mode-toggle"' not in body


def test_admin_mode_toggle_absent_on_admin_view_of_other_user(admin_client, test_user):
    body = admin_client.get(f"/admin/users/{test_user['id']}/profile").get_data(as_text=True)
    assert 'id="admin-mode-toggle"' not in body


# ---------------------------------------------------------------------------
# purge_conversations / set_store_conversations
# ---------------------------------------------------------------------------

def _make_conversation(app, entity_id, title="Chat"):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.message import Message
        conv = Conversation(entity_id=entity_id, title=title, model="test-model")
        db.session.add(conv)
        db.session.flush()
        db.session.add(Message(conversation_id=conv.id, role="user", content="hello"))
        db.session.commit()
        return conv.id


def _conversation_and_message_counts(app, entity_id):
    with app.app_context():
        from sqlalchemy import func, select

        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.message import Message
        convs = db.session.scalar(
            select(func.count(Conversation.id)).where(Conversation.entity_id == entity_id)
        )
        msgs = db.session.scalar(
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.entity_id == entity_id)
        )
        return convs, msgs


def test_purge_conversations_requires_login(client):
    resp = client.delete("/profile/conversations", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_purge_conversations_deletes_all_own(app, auth_client, test_user):
    _make_conversation(app, test_user["id"], title="One")
    _make_conversation(app, test_user["id"], title="Two")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        db.session.add(EntityStat(entity_id=test_user["id"], requests=0, input_tokens=0,
                                  output_tokens=0, cost=0, conversations=2))
        db.session.commit()

    resp = auth_client.delete("/profile/conversations")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["deleted"] == 2
    assert _conversation_and_message_counts(app, test_user["id"]) == (0, 0)

    # The lifetime conversation counter is a usage stat and survives the purge.
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        assert db.session.scalar(
            select(EntityStat.conversations).filter_by(entity_id=test_user["id"])
        ) == 2


def test_purge_conversations_does_not_touch_other_users(app, auth_client, test_user, admin_user):
    _make_conversation(app, test_user["id"])
    other_conv = _make_conversation(app, admin_user["id"], title="Admin Chat")

    resp = auth_client.delete("/profile/conversations")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["deleted"] == 1
    assert _conversation_and_message_counts(app, admin_user["id"]) == (1, 1)
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        assert db.session.get(Conversation, other_conv) is not None


def test_disable_store_conversations_sets_flag_and_purges(app, auth_client, test_user):
    _make_conversation(app, test_user["id"])

    resp = auth_client.post("/profile/settings/store-conversations", json={"enabled": False})
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["store_conversations"] is False
    assert data["deleted"] == 1
    assert _conversation_and_message_counts(app, test_user["id"]) == (0, 0)
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, test_user["id"]).store_conversations is False


def test_enable_store_conversations_no_purge(app, auth_client, test_user):
    _make_conversation(app, test_user["id"])
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        db.session.get(Entity, test_user["id"]).store_conversations = False
        db.session.commit()

    resp = auth_client.post("/profile/settings/store-conversations", json={"enabled": True})
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    assert data["store_conversations"] is True
    assert data["deleted"] == 0
    assert _conversation_and_message_counts(app, test_user["id"]) == (1, 1)
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, test_user["id"]).store_conversations is True


@pytest.mark.parametrize("payload", [{}, {"enabled": "yes"}, {"enabled": 1}])
def test_set_store_conversations_bad_payload(auth_client, payload):
    resp = auth_client.post("/profile/settings/store-conversations", json=payload)
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_set_store_conversations_requires_login(client):
    resp = client.post("/profile/settings/store-conversations", json={"enabled": True}, follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_project_detail_page_does_not_render_projects_section(app, auth_client, test_user):
    """A project's own detail page (which reuses _get_profile_data) must not
    waste a query building a project list for a project entity, and must not
    render the Projects section."""
    pid = _make_managed_project(app, test_user["id"], name="detail-svc")
    resp = auth_client.get(f"/projects/{pid}")
    assert resp.status_code == HTTPStatus.OK
    body = resp.get_data(as_text=True)
    assert 'id="project-table"' not in body



# ---------------------------------------------------------------------------
# user_consent — early access / per-requirement acknowledgement
# ---------------------------------------------------------------------------

def _make_ack_model(app, entity_id, model_name, needs_ack=False, early_access=False, end_date=None):
    """Create a public model with the given acknowledgement requirements."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(
            model_name=model_name,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            needs_ack=needs_ack,
            early_access=early_access,
            end_date=end_date,
        )
        db.session.add(mc)
        db.session.commit()
        db.session.refresh(mc)
        return {"id": mc.id, "model_name": mc.model_name}


def _consent_row(app, entity_id, model_id):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        row = db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=model_id)
        ).scalar_one_or_none()
        return None if row is None else {"consented_at": row.consented_at, "early_access_at": row.early_access_at}


def test_user_consent_needs_ack_only_sets_consented_at(app, auth_client, test_user):
    m = _make_ack_model(app, test_user["id"], "ack-only-model", needs_ack=True)
    assert auth_client.post(f"/profile/consent/{m['model_name']}").status_code == HTTPStatus.OK
    row = _consent_row(app, test_user["id"], m["id"])
    assert row["consented_at"] is not None
    assert row["early_access_at"] is None


def test_user_consent_early_only_sets_early_access_at(app, auth_client, test_user):
    m = _make_ack_model(app, test_user["id"], "early-only-model", early_access=True)
    assert auth_client.post(f"/profile/consent/{m['model_name']}").status_code == HTTPStatus.OK
    row = _consent_row(app, test_user["id"], m["id"])
    assert row["consented_at"] is None
    assert row["early_access_at"] is not None


def test_user_consent_both_sets_both(app, auth_client, test_user):
    m = _make_ack_model(app, test_user["id"], "both-ack-model", needs_ack=True, early_access=True)
    assert auth_client.post(f"/profile/consent/{m['model_name']}").status_code == HTTPStatus.OK
    row = _consent_row(app, test_user["id"], m["id"])
    assert row["consented_at"] is not None
    assert row["early_access_at"] is not None


def test_user_consent_fills_only_missing_requirement(app, auth_client, test_user):
    """A model gaining early_access after consent re-prompts; the second POST
    fills only early_access_at and preserves the original consented_at."""
    m = _make_ack_model(app, test_user["id"], "gains-early-model", needs_ack=True)
    auth_client.post(f"/profile/consent/{m['model_name']}")
    first = _consent_row(app, test_user["id"], m["id"])
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.get(ModelConfig, m["id"]).early_access = True
        db.session.commit()
    assert auth_client.post(f"/profile/consent/{m['model_name']}").status_code == HTTPStatus.OK
    row = _consent_row(app, test_user["id"], m["id"])
    assert row["consented_at"] == first["consented_at"]
    assert row["early_access_at"] is not None


def test_user_consent_expired_model_404(app, auth_client, test_user):
    from datetime import timedelta

    from lumen.timeutils import utcnow
    m = _make_ack_model(app, test_user["id"], "expired-ack-model", needs_ack=True,
                        end_date=utcnow() - timedelta(days=1))
    resp = auth_client.post(f"/profile/consent/{m['model_name']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
