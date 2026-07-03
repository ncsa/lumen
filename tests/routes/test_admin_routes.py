"""Admin route tests — focus on user management happy paths."""
from http import HTTPStatus

import pytest
from sqlalchemy import select


def test_valid_buckets_matches_periods():
    from lumen.blueprints.profile.routes import _USAGE_PERIODS, _VALID_BUCKETS
    assert _VALID_BUCKETS == {cfg["bucket"] for cfg in _USAGE_PERIODS.values()}


def test_toggle_user_flips_active(app, admin_client, test_user):
    resp = admin_client.post(f"/admin/users/{test_user['id']}/toggle")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["active"] is False
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, test_user["id"]).active is False


def test_reset_tokens_no_pool_returns_400(admin_client, test_user):
    resp = admin_client.post(f"/admin/users/{test_user['id']}/reset-tokens")
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_reset_tokens_unlimited_returns_400(app, admin_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"],
            max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()
    resp = admin_client.post(f"/admin/users/{test_user['id']}/reset-tokens")
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_reset_tokens_resets_balance(app, admin_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"],
            max_coins=500, refresh_coins=10, starting_coins=500,
        ))
        db.session.add(EntityBalance(entity_id=test_user["id"], coins_left=3))
        db.session.commit()

    resp = admin_client.post(f"/admin/users/{test_user['id']}/reset-tokens")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["coins_available"] == 500
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=test_user["id"])).scalar_one_or_none()
        assert float(bal.coins_left) == 500.0


def test_admin_user_profile_page(admin_client, test_user):
    resp = admin_client.get(f"/admin/users/{test_user['id']}/profile")
    assert resp.status_code == HTTPStatus.OK
    assert test_user["name"].encode() in resp.data


# ---------------------------------------------------------------------------
# /api/users — entity_stats integration
# ---------------------------------------------------------------------------

def test_api_users_returns_zeros_without_usage(admin_client, test_user):
    resp = admin_client.get("/admin/api/users")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    user = next(u for u in data["users"] if u["id"] == test_user["id"])
    assert user["requests"] == 0
    assert user["tokens_used"] == 0
    assert float(user["cost"]) == pytest.approx(0.0)
    assert user["last_used"] is None


def test_api_users_reflects_entity_stats(app, admin_client, test_user, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import update_stats
        update_stats(test_user["id"], test_model["id"], "chat", 100, 50, 0.03)
        db.session.commit()

    resp = admin_client.get("/admin/api/users")
    assert resp.status_code == HTTPStatus.OK
    data = resp.get_json()
    user = next(u for u in data["users"] if u["id"] == test_user["id"])
    assert user["requests"] == 1
    assert user["tokens_used"] == 150
    assert float(user["cost"]) == pytest.approx(0.03)
    assert user["last_used"] is not None


def test_api_users_sort_by_requests(app, admin_client, test_user, admin_user, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import update_stats
        update_stats(test_user["id"], test_model["id"], "chat", 10, 5, 0.001)
        db.session.commit()

    resp = admin_client.get("/admin/api/users?sort=requests&order=desc")
    assert resp.status_code == HTTPStatus.OK
    ids = [u["id"] for u in resp.get_json()["users"]]
    assert ids.index(test_user["id"]) < ids.index(admin_user["id"])


def test_config_post_backs_up_previous_config(app, admin_client, tmp_path):
    """Saving config writes the prior content to <config>.bak so a bad save is recoverable."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("app:\n  name: Original\n")
    original = app.config["CONFIG_YAML"]
    app.config["CONFIG_YAML"] = str(cfg)
    try:
        resp = admin_client.post("/admin/api/config", json={"app": {"name": "Updated"}})
        assert resp.status_code == HTTPStatus.OK
        bak = tmp_path / "config.yaml.bak"
        assert bak.exists()
        assert "Original" in bak.read_text()
        assert "Updated" in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_forbidden_when_editor_disabled(app, admin_client, tmp_path):
    """POST /admin/api/config returns 403 when CONFIG_EDITOR is False (git-managed config)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("app:\n  name: Original\n")
    original_path = app.config["CONFIG_YAML"]
    original_editor = app.config.get("CONFIG_EDITOR", True)
    app.config["CONFIG_YAML"] = str(cfg)
    app.config["CONFIG_EDITOR"] = False
    try:
        resp = admin_client.post("/admin/api/config", json={"app": {"name": "Updated"}})
        assert resp.status_code == HTTPStatus.FORBIDDEN
        # The file must be untouched when the editor is disabled.
        assert "Original" in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original_path
        app.config["CONFIG_EDITOR"] = original_editor


# A config with every secret-bearing path populated, for mask/restore tests.
_FULL_SECRET_CONFIG = """\
app:
  name: Lumen
  secret_key: real-secret-key
  encryption_key: real-encryption-key
  database:
    url: postgresql://user:realpass@host/db
oauth2:
  client_secret: real-oauth-secret
api:
  prometheus:
    enabled: true
    token: real-prom-token
  monitoring:
    token: real-mon-token
rate_limiting:
  storage_url: redis://:realredis@host:6379/0
models:
  - name: gpt-4o
    active: true
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-real-openai
"""


def _use_config(app, tmp_path, text):
    """Point CONFIG_YAML at a tmp file holding ``text``; return the path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    original = app.config["CONFIG_YAML"]
    app.config["CONFIG_YAML"] = str(cfg)
    return original, cfg


def test_config_get_masks_secrets(app, admin_client, tmp_path):
    """GET /admin/api/config replaces every secret with the MASK sentinel."""
    original, cfg = _use_config(app, tmp_path, _FULL_SECRET_CONFIG)
    try:
        resp = admin_client.get("/admin/api/config")
        assert resp.status_code == HTTPStatus.OK
        data = resp.get_json()
        assert data["app"]["secret_key"] == "********"
        assert data["app"]["encryption_key"] == "********"
        assert data["app"]["database"]["url"] == "********"
        assert data["oauth2"]["client_secret"] == "********"
        assert data["api"]["prometheus"]["token"] == "********"
        assert data["api"]["monitoring"]["token"] == "********"
        assert data["rate_limiting"]["storage_url"] == "********"
        assert data["models"][0]["endpoints"][0]["api_key"] == "********"
        # Non-secret fields are returned verbatim.
        assert data["app"]["name"] == "Lumen"
        assert data["models"][0]["name"] == "gpt-4o"
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_get_blanks_stay_blank(app, admin_client, tmp_path):
    """Empty/missing secrets stay blank, not MASK, so the UI shows 'not configured'."""
    original, cfg = _use_config(app, tmp_path, "app:\n  name: Lumen\n")
    try:
        resp = admin_client.get("/admin/api/config")
        assert resp.status_code == HTTPStatus.OK
        data = resp.get_json()
        app_section = data.get("app", {})
        assert app_section.get("secret_key", "") == ""
        assert app_section.get("encryption_key", "") == ""
        assert "database" not in app_section or app_section["database"].get("url", "") == ""
        assert "oauth2" not in data or data["oauth2"].get("client_secret", "") == ""
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_preserves_masked_secrets(app, admin_client, tmp_path):
    """Saving the masked payload back unchanged preserves real secrets on disk."""
    original, cfg = _use_config(app, tmp_path, _FULL_SECRET_CONFIG)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # POST the masked payload straight back (no field re-typed).
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        assert "real-secret-key" in on_disk
        assert "real-encryption-key" in on_disk
        assert "realpass@host" in on_disk
        assert "real-oauth-secret" in on_disk
        assert "real-prom-token" in on_disk
        assert "real-mon-token" in on_disk
        assert "realredis@host" in on_disk
        assert "sk-real-openai" in on_disk
        # And the literal sentinel never reaches disk.
        assert "********" not in on_disk
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_blank_deletes_secret(app, admin_client, tmp_path):
    """Clearing a secret field (omitting the key) deletes it on disk, not 'keep'."""
    original, cfg = _use_config(app, tmp_path, _FULL_SECRET_CONFIG)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # Clear the oauth2 client_secret: drop the key from the payload.
        masked["oauth2"].pop("client_secret", None)
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        assert "real-oauth-secret" not in on_disk
        assert "client_secret" not in on_disk
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_writes_new_secret(app, admin_client, tmp_path):
    """POSTing a real (non-sentinel) secret value writes it to disk."""
    original, cfg = _use_config(app, tmp_path, _FULL_SECRET_CONFIG)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        masked["app"]["secret_key"] = "brand-new-secret"
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        assert "brand-new-secret" in on_disk
        # Other secrets, still masked in the payload, are preserved from disk.
        assert "real-encryption-key" in on_disk
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_rejects_unrestorable_mask(app, admin_client, tmp_path):
    """A MASK whose model/url no longer matches on disk is rejected with 400 naming the field."""
    original, cfg = _use_config(app, tmp_path, _FULL_SECRET_CONFIG)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # Rename the model so no on-disk name match exists for the masked api_key.
        masked["models"][0]["name"] = "renamed-model"
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        msg = resp.get_json()["error"]
        assert "api_key" in msg
        # On-disk config is untouched (write never ran).
        assert "renamed-model" not in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_preserves_endpoint_api_keys(app, admin_client, tmp_path):
    """Endpoint api_keys survive a masked round-trip across multiple models/endpoints."""
    original, cfg = _use_config(app, tmp_path, _FULL_SECRET_CONFIG + """\
  - name: claude-3
    active: true
    endpoints:
      - url: https://api.anthropic.com/v1
        api_key: sk-ant-real
      - url: https://api.openai.com/v1
        api_key: sk-second-openai
""")
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        assert "sk-real-openai" in on_disk
        assert "sk-ant-real" in on_disk
        assert "sk-second-openai" in on_disk
        assert "********" not in on_disk
    finally:
        app.config["CONFIG_YAML"] = original
