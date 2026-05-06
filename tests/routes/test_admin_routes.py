"""Admin route tests — focus on config_managed enforcement plus core happy paths.

config_managed=True means the row is owned by config.yaml; the admin UI must
refuse to mutate it (otherwise UI edits would silently revert on the next
config sync, or worse, drift from yaml).
"""
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manual_group(app):
    """A group created via the admin UI (config_managed=False)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="manual-grp", config_managed=False, active=True)
        db.session.add(g)
        db.session.commit()
        return {"id": g.id, "name": g.name}


@pytest.fixture
def yaml_group(app):
    """A group synced from config.yaml (config_managed=True)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="yaml-grp", config_managed=True, active=True)
        db.session.add(g)
        db.session.commit()
        return {"id": g.id, "name": g.name}


# ---------------------------------------------------------------------------
# Group create / update / toggle
# ---------------------------------------------------------------------------

def test_create_group_redirects_to_detail(app, admin_client):
    resp = admin_client.post("/admin/groups", data={"name": "new-team", "description": "hi"},
                              follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        from lumen.models.group import Group
        g = Group.query.filter_by(name="new-team").first()
        assert g is not None
        assert g.config_managed is False
        assert f"/admin/groups/{g.id}" in resp.headers["Location"]


def test_create_group_empty_name_redirects_back(admin_client):
    resp = admin_client.post("/admin/groups", data={"name": "  "}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/groups" in resp.headers["Location"]


def test_update_manual_group_succeeds(app, admin_client, manual_group):
    resp = admin_client.post(
        f"/admin/groups/{manual_group['id']}",
        data={"name": "renamed", "description": "new desc"},
    )
    assert resp.status_code == 302
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = db.session.get(Group, manual_group["id"])
        assert g.name == "renamed"
        assert g.description == "new desc"


def test_update_yaml_group_forbidden(admin_client, yaml_group):
    resp = admin_client.post(
        f"/admin/groups/{yaml_group['id']}",
        data={"name": "renamed", "description": "x"},
    )
    assert resp.status_code == 403


def test_toggle_manual_group(app, admin_client, manual_group):
    resp = admin_client.post(f"/admin/groups/{manual_group['id']}/toggle")
    assert resp.status_code == 200
    assert resp.get_json()["active"] is False
    # Toggle again
    resp = admin_client.post(f"/admin/groups/{manual_group['id']}/toggle")
    assert resp.get_json()["active"] is True


def test_toggle_yaml_group_forbidden(admin_client, yaml_group):
    resp = admin_client.post(f"/admin/groups/{yaml_group['id']}/toggle")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Group membership
# ---------------------------------------------------------------------------

def test_add_member_to_manual_group(app, admin_client, manual_group, test_user):
    resp = admin_client.post(
        f"/admin/groups/{manual_group['id']}/members",
        data={"email": "testuser@example.com"},
    )
    assert resp.status_code == 302
    with app.app_context():
        from lumen.models.group_member import GroupMember
        m = GroupMember.query.filter_by(
            group_id=manual_group["id"], entity_id=test_user["id"]
        ).first()
        assert m is not None


def test_add_member_idempotent(app, admin_client, manual_group, test_user):
    """Adding the same user twice doesn't create duplicate membership rows."""
    admin_client.post(f"/admin/groups/{manual_group['id']}/members",
                       data={"email": "testuser@example.com"})
    admin_client.post(f"/admin/groups/{manual_group['id']}/members",
                       data={"email": "testuser@example.com"})
    with app.app_context():
        from lumen.models.group_member import GroupMember
        count = GroupMember.query.filter_by(
            group_id=manual_group["id"], entity_id=test_user["id"]
        ).count()
        assert count == 1


def test_add_member_to_yaml_group_forbidden(admin_client, yaml_group, test_user):
    resp = admin_client.post(
        f"/admin/groups/{yaml_group['id']}/members",
        data={"email": "testuser@example.com"},
    )
    assert resp.status_code == 403


def test_add_member_unknown_email_silently_ignored(app, admin_client, manual_group):
    """Unknown emails don't error; route just redirects without creating a row."""
    resp = admin_client.post(
        f"/admin/groups/{manual_group['id']}/members",
        data={"email": "not-a-user@example.com"},
    )
    assert resp.status_code == 302
    with app.app_context():
        from lumen.models.group_member import GroupMember
        assert GroupMember.query.filter_by(group_id=manual_group["id"]).count() == 0


def test_remove_config_managed_member_forbidden(app, admin_client, manual_group, test_user):
    """A config-managed membership row cannot be removed via the admin UI."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        m = GroupMember(
            group_id=manual_group["id"],
            entity_id=test_user["id"],
            config_managed=True,
        )
        db.session.add(m)
        db.session.commit()
        member_id = m.id

    resp = admin_client.post(
        f"/admin/groups/{manual_group['id']}/members/{member_id}/remove"
    )
    assert resp.status_code == 403


def test_toggle_user_flips_active(app, admin_client, test_user):
    resp = admin_client.post(f"/admin/users/{test_user['id']}/toggle")
    assert resp.status_code == 200
    assert resp.get_json()["active"] is False
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, test_user["id"]).active is False


def test_reset_tokens_no_pool_returns_400(admin_client, test_user):
    resp = admin_client.post(f"/admin/users/{test_user['id']}/reset-tokens")
    assert resp.status_code == 400


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
    assert resp.status_code == 400


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
    assert resp.status_code == 200
    assert resp.get_json()["coins_available"] == 500
    with app.app_context():
        from lumen.models.entity_balance import EntityBalance
        bal = EntityBalance.query.filter_by(entity_id=test_user["id"]).first()
        assert float(bal.coins_left) == 500.0


def test_admin_user_usage_page(admin_client, test_user):
    resp = admin_client.get(f"/admin/users/{test_user['id']}/usage")
    assert resp.status_code == 200
    assert test_user["name"].encode() in resp.data
