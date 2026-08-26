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


def test_update_user_requires_admin(auth_client, test_user):
    resp = auth_client.patch(f"/admin/users/{test_user['id']}", json={"active": False})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_update_user_sets_active_and_coins(app, admin_client, test_user):
    resp = admin_client.patch(
        f"/admin/users/{test_user['id']}", json={"active": False, "max_coins": 200, "refresh_coins": 5}
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["active"] is False
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_limit import EntityLimit
        assert db.session.get(Entity, test_user["id"]).active is False
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=test_user["id"])
        ).scalar_one_or_none()
        assert float(limit.max_coins) == 200.0
        assert float(limit.refresh_coins) == 5.0
        assert limit.config_managed is False


def test_update_user_updates_existing_limit(app, admin_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=100, refresh_coins=1, starting_coins=100,
            config_managed=True,
        ))
        db.session.commit()
    resp = admin_client.patch(f"/admin/users/{test_user['id']}", json={"refresh_coins": 3})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=test_user["id"])
        ).scalar_one_or_none()
        assert float(limit.max_coins) == 100.0
        assert float(limit.refresh_coins) == 3.0
        # Editing only refresh leaves starting untouched.
        assert float(limit.starting_coins) == 100.0
        assert limit.config_managed is False


def test_update_user_max_updates_starting_for_reset(app, admin_client, test_user):
    """Raising Max Coins must also raise what reset-tokens refills to."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=100, refresh_coins=1, starting_coins=100,
        ))
        db.session.commit()
    resp = admin_client.patch(f"/admin/users/{test_user['id']}", json={"max_coins": 500})
    assert resp.status_code == HTTPStatus.OK

    resp = admin_client.post(f"/admin/users/{test_user['id']}/reset-tokens")
    assert resp.status_code == HTTPStatus.OK
    assert float(resp.get_json()["coins_available"]) == 500.0


def test_update_user_blank_coins_clears_limit(app, admin_client, test_user):
    """Blanking both coin fields removes the user's own pool (falls back to defaults)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=100, refresh_coins=1, starting_coins=100,
        ))
        db.session.commit()
    resp = admin_client.patch(
        f"/admin/users/{test_user['id']}", json={"max_coins": "", "refresh_coins": ""}
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=test_user["id"])
        ).scalar_one_or_none()
        assert limit is None


def test_update_user_blank_max_alone_clears_limit(app, admin_client, test_user):
    """A blank Max Coins clears the pool even when a refresh value is sent along."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=100, refresh_coins=1, starting_coins=100,
        ))
        db.session.commit()
    resp = admin_client.patch(
        f"/admin/users/{test_user['id']}", json={"max_coins": "", "refresh_coins": 5}
    )
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=test_user["id"])
        ).scalar_one_or_none()
        assert limit is None


def test_update_user_invalid_coins_return_400(admin_client, test_user):
    for payload in (
        {"max_coins": -1, "refresh_coins": 0},
        {"max_coins": 10, "refresh_coins": -1},
        {"max_coins": "abc", "refresh_coins": 1},
    ):
        resp = admin_client.patch(f"/admin/users/{test_user['id']}", json=payload)
        assert resp.status_code == HTTPStatus.BAD_REQUEST, payload


def test_update_user_ignores_name(app, admin_client, test_user):
    resp = admin_client.patch(f"/admin/users/{test_user['id']}", json={"name": "New Name", "active": True})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, test_user["id"]).name == test_user["name"]


def test_admin_user_profile_page(admin_client, test_user):
    resp = admin_client.get(f"/admin/users/{test_user['id']}/profile")
    assert resp.status_code == HTTPStatus.OK
    assert test_user["name"].encode() in resp.data


# ---------------------------------------------------------------------------
# /api/users — entity_stats integration
# ---------------------------------------------------------------------------

def test_api_users_includes_limit_fields(app, admin_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=300, refresh_coins=2, starting_coins=300,
        ))
        db.session.commit()
    resp = admin_client.get("/admin/api/users")
    row = next(u for u in resp.get_json()["users"] if u["id"] == test_user["id"])
    assert row["max_coins"] == 300.0
    assert row["refresh_coins"] == 2.0


def test_api_users_shows_group_inherited_unlimited_pool(app, admin_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_limit import GroupLimit
        from lumen.models.group_member import GroupMember

        group = Group(name="unlimited-users", active=True)
        db.session.add(group)
        db.session.flush()
        db.session.add_all([
            GroupMember(entity_id=test_user["id"], group_id=group.id),
            GroupLimit(group_id=group.id, max_coins=-2, refresh_coins=0, starting_coins=0),
        ])
        db.session.commit()

    resp = admin_client.get("/admin/api/users")
    row = next(u for u in resp.get_json()["users"] if u["id"] == test_user["id"])
    assert row["max_coins"] is None
    assert row["coins_available"] == -2


def test_api_users_own_pool_overrides_group_unlimited(app, admin_client, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.group import Group
        from lumen.models.group_limit import GroupLimit
        from lumen.models.group_member import GroupMember

        group = Group(name="overridden-unlimited-users", active=True)
        db.session.add(group)
        db.session.flush()
        db.session.add_all([
            GroupMember(entity_id=test_user["id"], group_id=group.id),
            GroupLimit(group_id=group.id, max_coins=-2, refresh_coins=0, starting_coins=0),
            EntityLimit(entity_id=test_user["id"], max_coins=100, refresh_coins=1, starting_coins=100),
        ])
        db.session.commit()

    resp = admin_client.get("/admin/api/users")
    row = next(u for u in resp.get_json()["users"] if u["id"] == test_user["id"])
    assert row["max_coins"] == 100
    assert row["coins_available"] == 0


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
        resp = admin_client.post("/admin/api/config", json={"version": 3, "app": {"name": "Updated"}})
        assert resp.status_code == HTTPStatus.OK
        bak = tmp_path / "config.yaml.bak"
        assert bak.exists()
        assert "Original" in bak.read_text()
        assert "Updated" in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_rejects_removed_policy_keys(app, admin_client, tmp_path):
    """The editor cannot save version-2 policy that version 3 would ignore."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("version: 3\napp:\n  name: Original\n")
    original = app.config["CONFIG_YAML"]
    app.config["CONFIG_YAML"] = str(cfg)
    try:
        resp = admin_client.post(
            "/admin/api/config",
            json={"version": 3, "groups": {"staff": {"rules": []}}},
        )
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert "groups:" in resp.get_json()["error"]
        assert "groups:" not in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_succeeds_when_backup_unwritable(app, admin_client, tmp_path):
    """A failed .bak copy (e.g. read-only dir in a container) must not block the save."""
    from unittest.mock import patch
    cfg = tmp_path / "config.yaml"
    cfg.write_text("app:\n  name: Original\n")
    original = app.config["CONFIG_YAML"]
    app.config["CONFIG_YAML"] = str(cfg)
    try:
        with patch("lumen.commands.shutil.copy2", side_effect=PermissionError(13, "Permission denied")):
            resp = admin_client.post("/admin/api/config", json={"version": 3, "app": {"name": "Updated"}})
        assert resp.status_code == HTTPStatus.OK
        assert "Updated" in cfg.read_text()
        assert not (tmp_path / "config.yaml.bak").exists()
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
        resp = admin_client.post("/admin/api/config", json={"version": 3, "app": {"name": "Updated"}})
        assert resp.status_code == HTTPStatus.FORBIDDEN
        # The file must be untouched when the editor is disabled.
        assert "Original" in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original_path
        app.config["CONFIG_EDITOR"] = original_editor


# A config with every secret-bearing path populated, for mask/restore tests.
_FULL_SECRET_CONFIG = """\
version: 3
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
    input_cost_per_million: 1
    output_cost_per_million: 1
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
    input_cost_per_million: 1
    output_cost_per_million: 1
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


def test_config_post_preserves_duplicate_url_endpoints(app, admin_client, tmp_path):
    """Two endpoints sharing a URL (documented round-robin multi-key) round-trip by position."""
    config = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: gpt-4o
    input_cost_per_million: 1
    output_cost_per_million: 1
    active: true
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-first
      - url: https://api.openai.com/v1
        api_key: sk-second
"""
    original, cfg = _use_config(app, tmp_path, config)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # Both keys masked on GET.
        assert masked["models"][0]["endpoints"][0]["api_key"] == "********"
        assert masked["models"][0]["endpoints"][1]["api_key"] == "********"
        # POST the masked payload straight back — both keys restored by position.
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        assert "sk-first" in on_disk
        assert "sk-second" in on_disk
        assert "********" not in on_disk
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_rejects_duplicate_model_names(app, admin_client, tmp_path):
    """Duplicate model names on disk → ambiguous restore → 400, no silent key swap."""
    config = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: gpt-4o
    input_cost_per_million: 1
    output_cost_per_million: 1
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-first
  - name: gpt-4o
    input_cost_per_million: 1
    output_cost_per_million: 1
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-second
"""
    original, cfg = _use_config(app, tmp_path, config)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        # Disk untouched — no silent key swap.
        assert "********" not in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_remove_endpoint_preserves_remaining_key(app, admin_client, tmp_path):
    """Removing an endpoint restores the remaining endpoint's OWN key, not the deleted one's."""
    config = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: gpt-4o
    input_cost_per_million: 1
    output_cost_per_million: 1
    active: true
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-first
      - url: https://api.anthropic.com/v1
        api_key: sk-second
"""
    original, cfg = _use_config(app, tmp_path, config)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # Drop the first endpoint (simulate admin clicking ✕ on row 0).
        masked["models"][0]["endpoints"].pop(0)
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        # The remaining endpoint (url=anthropic) must keep its own key, not sk-first.
        assert "sk-second" in on_disk
        assert "sk-first" not in on_disk
        assert "********" not in on_disk
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_reorder_endpoints_preserves_keys(app, admin_client, tmp_path):
    """Reordering endpoints restores each to its OWN key by URL, not by position."""
    config = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: gpt-4o
    input_cost_per_million: 1
    output_cost_per_million: 1
    active: true
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-openai
      - url: https://api.anthropic.com/v1
        api_key: sk-anthropic
"""
    original, cfg = _use_config(app, tmp_path, config)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # Swap the two endpoints.
        eps = masked["models"][0]["endpoints"]
        eps[0], eps[1] = eps[1], eps[0]
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.OK
        on_disk = cfg.read_text()
        # Each URL must be paired with its own key after the round-trip.
        assert "sk-openai" in on_disk
        assert "sk-anthropic" in on_disk
        assert "********" not in on_disk
    finally:
        app.config["CONFIG_YAML"] = original


def test_config_post_duplicate_url_count_mismatch_rejects(app, admin_client, tmp_path):
    """Adding/removing within a duplicate-URL group → 400, not silent corruption."""
    config = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: gpt-4o
    input_cost_per_million: 1
    output_cost_per_million: 1
    active: true
    endpoints:
      - url: https://api.openai.com/v1
        api_key: sk-first
      - url: https://api.openai.com/v1
        api_key: sk-second
"""
    original, cfg = _use_config(app, tmp_path, config)
    try:
        masked = admin_client.get("/admin/api/config").get_json()
        # Drop one of the two same-URL endpoints — count mismatch → ambiguous.
        masked["models"][0]["endpoints"].pop(0)
        resp = admin_client.post("/admin/api/config", json=masked)
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert "********" not in cfg.read_text()
    finally:
        app.config["CONFIG_YAML"] = original
