"""The API key access matrix: what each key type and the monitor token may do (SQLite).

Columns are the credential (admin key, normal key, monitoring key, the
deprecated ``api.monitoring.token``), rows the action. Every cell is its own
parametrised case; the normal-key cells double as the regression check. The
admin API row (``/v1/organization/*``) is here too; its own tests are in
``tests/routes/test_admin_api.py``.
"""
from datetime import timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import func, select

from lumen.extensions import db, limiter
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.models.entity import Entity
from lumen.models.entity_limit import EntityLimit
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services import model_cache
from lumen.services.crypto import hash_api_key
from lumen.timeutils import utcnow
from tests.conftest import set_model_owner

MONITOR = "matrix-monitor-token"
TOKENS = {
    "admin": "sk_admin_matrix_test_key_000000000000000000",
    "normal": "sk_matrix_test_key_0000000000000000000000000",
    "monitoring": "sk_monitor_matrix_test_key_00000000000000000",
    "token": MONITOR,
}
KEY_TYPES = {"admin": KEY_TYPE_ADMIN, "normal": KEY_TYPE_NORMAL, "monitoring": KEY_TYPE_MONITORING}
CREDENTIALS = list(TOKENS)
MONITOR_PATH = ("admin", "monitoring", "token")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_metrics")

# Every other /v1 route: (method, path, request kwargs, normal-key status). A
# normal key passes authentication; the empty body then fails the view's own
# validation (400), except acknowledging a model, which needs no body.
PROXY_ROUTES = [
    ("post", "/v1/chat/completions", {"json": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/completions", {"json": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/embeddings", {"json": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/audio/transcriptions", {"data": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/audio/translations", {"data": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/messages", {"json": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/messages/count_tokens", {"json": {}}, HTTPStatus.BAD_REQUEST),
    ("post", "/v1/models/open-model/acknowledge", {"json": {}}, HTTPStatus.OK),
]


@pytest.fixture
def monitor_token(app):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}), "api": {"monitoring": {"token": MONITOR}}}
    yield MONITOR
    app.config["YAML_DATA"].pop("api", None)


@pytest.fixture
def keys(app, admin_user, monitor_token):
    """One key of each type, all owned by admin_user (in ``admins``, unlimited pool)."""
    with app.app_context():
        db.session.add(EntityLimit(entity_id=admin_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0))
        ids = {}
        for name, key_type in KEY_TYPES.items():
            key = APIKey(entity_id=admin_user["id"], name=name, key_type=key_type,
                         key_hash=hash_api_key(TOKENS[name]))
            db.session.add(key)
            db.session.flush()
            ids[name] = key.id
        db.session.commit()
    return ids


@pytest.fixture
def models(app, keys):
    """open-model (usable by admin_user) and private-model (owned by someone else)."""
    with app.app_context():
        outsider = Entity(entity_type="user", email="outsider@example.com", name="Outsider", active=True)
        db.session.add(outsider)
        for name in ("open-model", "private-model"):
            m = ModelConfig(model_name=name, input_cost_per_million=1.0, output_cost_per_million=2.0)
            db.session.add(m)
            db.session.flush()
            db.session.add(ModelEndpoint(model_config_id=m.id, url="http://localhost:9999/v1",
                                         api_key="k", model_name="dummy", healthy=True))
        db.session.commit()
        private_id = db.session.execute(select(ModelConfig.id).filter_by(model_name="private-model")).scalar_one()
        set_model_owner(private_id, outsider.id)
    model_cache.clear()


def _auth(credential):
    return {"Authorization": f"Bearer {TOKENS[credential]}"}


def _request_logs(app):
    with app.app_context():
        return db.session.execute(select(func.count()).select_from(RequestLog)).scalar()


# ---------------------------------------------------------------------------
# Proxy routes: chat, completions, embeddings, audio, Anthropic, acknowledge
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", CREDENTIALS)
@pytest.mark.parametrize("method,path,kwargs,normal_status", PROXY_ROUTES)
def test_proxy_routes(client, models, credential, method, path, kwargs, normal_status):
    resp = getattr(client, method)(path, headers=_auth(credential), **kwargs)
    if credential == "normal":
        assert resp.status_code == normal_status
    else:
        assert resp.status_code == HTTPStatus.FORBIDDEN
        assert resp.get_json()["error"]["type"] in ("permission_error", "authentication_error")


@pytest.mark.parametrize("credential", ["admin", "monitoring"])
def test_wrong_type_message_names_the_key_type(client, models, credential):
    resp = client.post("/v1/chat/completions", headers=_auth(credential), json={})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"] == {
        "message": f"{credential.capitalize()} keys can only access /v1/models",
        "type": "permission_error",
    }


def test_anthropic_x_api_key_admin_key_is_refused(client, models):
    resp = client.post("/v1/messages", headers={"x-api-key": TOKENS["admin"]}, json={})
    assert resp.status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# GET /v1/usage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", CREDENTIALS)
def test_usage(client, models, credential):
    resp = client.get("/v1/usage", headers=_auth(credential))
    if credential == "normal":
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json()["requests"] == 0
    else:
        assert resp.status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# GET /v1/models and /v1/models/<id>
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", CREDENTIALS)
def test_list_models(client, models, credential):
    resp = client.get("/v1/models", headers=_auth(credential))
    assert resp.status_code == HTTPStatus.OK
    ids = {m["id"] for m in resp.get_json()["data"]}
    if credential == "normal":
        assert ids == {"open-model"}
    else:
        assert ids == {"open-model", "private-model"}


@pytest.mark.parametrize("credential", CREDENTIALS)
def test_get_model(client, models, credential):
    assert client.get("/v1/models/open-model", headers=_auth(credential)).status_code == HTTPStatus.OK
    resp = client.get("/v1/models/private-model", headers=_auth(credential))
    expected = HTTPStatus.NOT_FOUND if credential == "normal" else HTTPStatus.OK
    assert resp.status_code == expected


@pytest.mark.parametrize("credential", MONITOR_PATH)
def test_monitor_path_models_have_no_consent_state(client, models, credential):
    model = client.get("/v1/models/open-model", headers=_auth(credential)).get_json()
    assert "tags" not in model


# ---------------------------------------------------------------------------
# ?tokens=true on the models endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", CREDENTIALS)
def test_tokens_parameter(client, models, credential):
    listed = client.get("/v1/models?tokens=true", headers=_auth(credential)).get_json()["data"]
    one = client.get("/v1/models/open-model?tokens=true", headers=_auth(credential)).get_json()
    for model in [*listed, one]:
        assert "requests" in model
        if credential == "normal":
            assert not set(TOKEN_FIELDS) & model.keys()
        else:
            assert set(TOKEN_FIELDS) <= model.keys()


def test_normal_key_with_tokens_matches_the_plain_response(client, models):
    for path in ("/v1/models", "/v1/models/open-model"):
        plain = client.get(path, headers=_auth("normal")).get_json()
        with_tokens = client.get(f"{path}?tokens=true", headers=_auth("normal")).get_json()
        assert with_tokens == plain


# ---------------------------------------------------------------------------
# Missing, revoked, expired keys; header errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/models", "/v1/usage", "/v1/chat/completions"])
def test_missing_header_400(client, models, path):
    resp = client.get(path) if path != "/v1/chat/completions" else client.post(path, json={})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize("credential", list(KEY_TYPES))
@pytest.mark.parametrize("field", ["revoked_at", "expires_at"])
def test_revoked_or_expired_key_401(app, client, keys, models, credential, field):
    with app.app_context():
        setattr(db.session.get(APIKey, keys[credential]), field, utcnow() - timedelta(seconds=1))
        db.session.commit()
    for resp in (client.get("/v1/models", headers=_auth(credential)),
                 client.post("/v1/chat/completions", headers=_auth(credential), json={})):
        assert resp.status_code == HTTPStatus.UNAUTHORIZED
        assert resp.get_json()["error"]["type"] == "authentication_error"


def test_unknown_key_401(client, models):
    resp = client.get("/v1/models", headers={"Authorization": "Bearer sk_admin_unknown"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# Owner check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", ["admin", "monitoring"])
def test_owner_removed_from_admins_401_and_restored(app, client, models, credential):
    admins = app.config["YAML_DATA"]["admins"]
    app.config["YAML_DATA"]["admins"] = []
    try:
        assert client.get("/v1/models", headers=_auth(credential)).status_code == HTTPStatus.UNAUTHORIZED
    finally:
        app.config["YAML_DATA"]["admins"] = admins
    assert client.get("/v1/models", headers=_auth(credential)).status_code == HTTPStatus.OK


@pytest.mark.parametrize("credential", ["admin", "monitoring"])
def test_owner_deactivated_401_and_restored(app, client, admin_user, models, credential):
    def set_active(active):
        with app.app_context():
            db.session.get(Entity, admin_user["id"]).active = active
            db.session.commit()

    set_active(False)
    assert client.get("/v1/models", headers=_auth(credential)).status_code == HTTPStatus.UNAUTHORIZED
    set_active(True)
    assert client.get("/v1/models", headers=_auth(credential)).status_code == HTTPStatus.OK


def test_normal_key_of_deactivated_owner_still_403(app, client, admin_user, models):
    with app.app_context():
        db.session.get(Entity, admin_user["id"]).active = False
        db.session.commit()
    resp = client.get("/v1/models", headers=_auth("normal"))
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.get_json()["error"]["message"] == "Account disabled"


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", ["admin", "monitoring"])
def test_admin_and_monitoring_keys_are_never_billed(app, client, keys, models, credential):
    for path in ("/v1/models", "/v1/models?tokens=true", "/v1/models/open-model"):
        assert client.get(path, headers=_auth(credential)).status_code == HTTPStatus.OK
    client.post("/v1/chat/completions", headers=_auth(credential), json={"model": "open-model"})
    assert _request_logs(app) == 0
    with app.app_context():
        key = db.session.get(APIKey, keys[credential])
        # Each authenticated request is a use (the refused chat call too), but nothing is billed.
        assert (key.requests, key.input_tokens, key.output_tokens, key.cost) == (4, 0, 0, 0)


# ---------------------------------------------------------------------------
# Admin API: /v1/organization/*
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("credential", CREDENTIALS)
def test_admin_api(app, client, keys, admin_user, credential):
    with app.app_context():
        limiter.reset()
    resp = client.get(f"/v1/organization/users/user-{admin_user['uuid']}", headers=_auth(credential))
    if credential == "admin":
        assert resp.status_code == HTTPStatus.OK
    else:
        assert resp.status_code == HTTPStatus.FORBIDDEN
        assert resp.get_json()["error"]["type"] == "permission_error"


# ---------------------------------------------------------------------------
# Use count of admin and monitoring keys
# ---------------------------------------------------------------------------

def _uses(app, key_id):
    with app.app_context():
        key = db.session.get(APIKey, key_id)
        return key.requests, key.last_used_at


@pytest.mark.parametrize("credential", ["admin", "monitoring"])
def test_each_models_call_is_counted(app, client, keys, models, credential):
    before = utcnow()
    for _ in range(2):
        assert client.get("/v1/models", headers=_auth(credential)).status_code == HTTPStatus.OK
    requests, last_used_at = _uses(app, keys[credential])
    assert requests == 2
    assert before <= last_used_at <= utcnow()


@pytest.mark.parametrize("credential", ["admin", "monitoring"])
def test_call_refused_for_the_key_type_is_counted(app, client, keys, models, credential):
    resp = client.get("/v1/usage", headers=_auth(credential))
    assert resp.status_code == HTTPStatus.FORBIDDEN
    requests, last_used_at = _uses(app, keys[credential])
    assert requests == 1
    assert last_used_at is not None


def test_normal_key_models_call_is_not_counted(app, client, keys, models):
    assert client.get("/v1/models", headers=_auth("normal")).status_code == HTTPStatus.OK
    assert _uses(app, keys["normal"]) == (0, None)
