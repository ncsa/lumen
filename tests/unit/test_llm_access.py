import pytest

from lumen.services.llm import get_model_access, get_model_access_status, get_pool_limit


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
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_user_explicitly_allowed(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="whitelist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_group_blacklist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_graylist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="graylist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "graylist"


def test_group_whitelist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="whitelist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_user_allowed_overrides_group_blacklist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="blacklist"))
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="whitelist"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_blacklist_blocks_chat(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is False


def test_graylist_blocks_chat_without_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="graylist"))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is False


def test_graylist_allows_chat_with_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from datetime import datetime, timezone
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.entity_model_consent import EntityModelConsent
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="graylist"))
        db.session.add(EntityModelConsent(
            entity_id=entity_id,
            model_config_id=model_id,
            consented_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


def test_whitelist_allows_chat(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="whitelist"))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


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


# ---------------------------------------------------------------------------
# Entity-level model_access_default (used for client/service entities via yaml)
# Covers llm.py lines 126-133: the fallthrough after no user or group rules.
# ---------------------------------------------------------------------------

def test_entity_model_access_default_blacklist(app, ids):
    """Entity model_access_default=blacklist blocks when no user/group rules exist."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.get(Entity, entity_id)
        entity.model_access_default = "blacklist"
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_entity_model_access_default_graylist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.get(Entity, entity_id)
        entity.model_access_default = "graylist"
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "graylist"


def test_entity_model_access_default_whitelist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.get(Entity, entity_id)
        entity.model_access_default = "whitelist"
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"
