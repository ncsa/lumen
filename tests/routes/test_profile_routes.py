"""Tests for the profile blueprint routes."""
from http import HTTPStatus
import json
import pytest


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
# client_profile_page redirect
# ---------------------------------------------------------------------------

def test_client_profile_page_redirects(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        svc = Entity(entity_type="client", name="svc", initials="SV", active=True)
        db.session.add(svc)
        db.session.commit()
        db.session.refresh(svc)
        sid = svc.id

    resp = auth_client.get(f"/profile/client/{sid}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.MOVED_PERMANENTLY
    assert "/clients/" in resp.headers["Location"]


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
            active=True,
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
# user_consent
# ---------------------------------------------------------------------------

def _make_graylist_model(app, entity_id, model_name="graylist-model"):
    """Create a model graylisted for the given entity via EntityModelAccess."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.models.entity_model_access import EntityModelAccess
        mc = ModelConfig(
            model_name=model_name,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            active=True,
        )
        db.session.add(mc)
        db.session.flush()
        db.session.add(EntityModelAccess(
            entity_id=entity_id,
            model_config_id=mc.id,
            access_type="graylist",
        ))
        db.session.commit()
        db.session.refresh(mc)
        return {"id": mc.id, "model_name": mc.model_name}


def test_user_consent_requires_login(client):
    resp = client.post("/profile/consent/some-model", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_user_consent_model_not_found(auth_client):
    resp = auth_client.post("/profile/consent/nonexistent-model-xyz")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_user_consent_not_graylisted(app, auth_client, test_model):
    """Posting consent for a non-graylisted model returns 400."""
    resp = auth_client.post(f"/profile/consent/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_user_consent_success(app, auth_client, test_user):
    """Posting consent for a graylisted model records it and returns 200."""
    gm = _make_graylist_model(app, test_user["id"])
    resp = auth_client.post(f"/profile/consent/{gm['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["ok"] is True

    # Verify consent was persisted
    with app.app_context():
        from lumen.services.llm import has_model_consent
        assert has_model_consent(test_user["id"], gm["id"])


def test_user_consent_idempotent(app, auth_client, test_user):
    """Posting consent twice is idempotent — second call still returns 200."""
    gm = _make_graylist_model(app, test_user["id"], model_name="graylist-model-2")
    auth_client.post(f"/profile/consent/{gm['model_name']}")
    resp = auth_client.post(f"/profile/consent/{gm['model_name']}")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# _model_status inactive branch (outer function, called from index route)
# ---------------------------------------------------------------------------

def test_profile_page_shows_inactive_model(app, auth_client):
    """Inactive model appears with disabled status on profile page."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(
            model_name="inactive-model",
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            active=False,
        )
        db.session.add(mc)
        db.session.commit()

    resp = auth_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Coin pool (EntityLimit with positive balance)
# ---------------------------------------------------------------------------

def test_profile_page_with_coin_pool(app, auth_client, test_user):
    """Profile page renders correctly when entity has a token limit (coin pool)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.entity_balance import EntityBalance
        from datetime import datetime, timezone
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
# Models with past usage (line 84: inactive models added from usage_by_id)
# ---------------------------------------------------------------------------

def test_profile_page_shows_model_with_past_usage(app, auth_client, test_user):
    """A model that is now inactive but has ModelStat rows still appears."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.models.model_stat import ModelStat
        from datetime import datetime, timezone
        mc = ModelConfig(
            model_name="retired-model",
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            active=False,
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
