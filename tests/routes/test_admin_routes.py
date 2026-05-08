"""Admin route tests — focus on user management happy paths."""


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
