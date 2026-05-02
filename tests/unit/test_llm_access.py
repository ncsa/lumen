import pytest

from lumen.services.llm import get_model_access_status, get_pool_limit


@pytest.fixture
def ids(app, test_user, test_model):
    return test_user["id"], test_model["id"]


def test_default_no_rules_is_allowed(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_user_explicitly_blocked(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, allowed=False))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_user_explicitly_allowed(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, allowed=True))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_global_blacklist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_global_graylist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(model_config_id=model_id, access_type="graylist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "graylist"


def test_global_whitelist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(model_config_id=model_id, access_type="whitelist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_user_allowed_overrides_global_blacklist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.global_model_access import GlobalModelAccess
        db.session.add(GlobalModelAccess(model_config_id=model_id, access_type="blacklist"))
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, allowed=True))
        db.session.commit()
        # User-level access takes precedence over global blacklist
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_get_pool_limit_no_limit(app, ids):
    entity_id, _ = ids
    with app.app_context():
        assert get_pool_limit(entity_id) is None


def test_get_pool_limit_with_limit(app, ids):
    entity_id, _ = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=10, starting_coins=100))
        db.session.commit()
        result = get_pool_limit(entity_id)
        assert result is not None
        assert result[0] == 100.0


def test_get_pool_limit_unlimited(app, ids):
    entity_id, _ = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        assert get_pool_limit(entity_id) == (-2, 0, 0)


def test_get_pool_limit_blocked(app, ids):
    entity_id, _ = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=0, refresh_coins=0, starting_coins=0))
        db.session.commit()
        assert get_pool_limit(entity_id) is None
