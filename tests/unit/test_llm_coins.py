"""Tests for subtract_coins, deduct_coins, get_model_access (graylist path), has_model_consent."""
from sqlalchemy import select


def test_subtract_coins_deducts_balance(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert float(bal.coins_left) == 90.0


def test_subtract_coins_noop_when_blocked(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.services.llm import subtract_coins
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        # No exception, just no-op
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()


def test_subtract_coins_noop_when_unlimited(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()


def test_subtract_coins_deducts_correct_amount(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        subtract_coins(entity_id, model_id, 5.0)
        db.session.commit()
        bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert float(bal.coins_left) == 95.0


def test_get_model_access_graylist_without_consent(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        from lumen.services.llm import get_model_access
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="graylist"))
        db.session.commit()
        # Graylist without consent → access denied
        assert get_model_access(entity_id, model_id) is False


def test_get_model_access_graylist_with_consent(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from datetime import datetime, timezone
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        from lumen.services.llm import get_model_access
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="graylist"))
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, consented_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


def test_has_model_consent_true(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from datetime import datetime, timezone
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.services.llm import has_model_consent
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, consented_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.session.commit()
        assert has_model_consent(entity_id, model_id) is True


def test_has_model_consent_false(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.services.llm import has_model_consent
        assert has_model_consent(entity_id, model_id) is False


def test_subtract_coins_noop_when_no_balance_row(app, test_user, test_model):
    """subtract_coins is a no-op when no EntityBalance row exists (limit present but balance not yet created)."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import subtract_coins
        from sqlalchemy import select
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.commit()
        # No EntityBalance row — subtract_coins must not raise and must not create one
        subtract_coins(entity_id, model_id, 10.0)
        db.session.commit()
        assert db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none() is None


