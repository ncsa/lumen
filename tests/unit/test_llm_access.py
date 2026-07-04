import pytest

from lumen.services.llm import get_model_access, get_model_access_status, get_pool_limit


@pytest.fixture
def ids(app, test_user, test_model):
    return test_user["id"], test_model["id"]


def _set_needs_ack(app, model_id):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.needs_ack = True
    db.session.commit()


def test_default_no_rules_is_allowed(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_user_explicitly_blocked(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blocked"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_user_explicitly_allowed(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
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
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="blocked"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_needs_ack(app, ids):
    """needs_ack is a model-level property: an allowed group rule on a needs_ack model resolves to needs_ack."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        _set_needs_ack(app, model_id)
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=entity_id, group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


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
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="allowed"))
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
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=model_id, access_type="blocked"))
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_blacklist_blocks_chat(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blocked"))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is False


def test_needs_ack_blocks_chat_without_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is False


def test_needs_ack_allows_chat_with_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from datetime import datetime, timezone
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.entity_model_consent import EntityModelConsent
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
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
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


def test_disabled_model_not_overridable(app, ids):
    """A disabled model is blocked even with an explicit user allow rule."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.model_config import ModelConfig
        mc = db.session.get(ModelConfig, model_id)
        mc.disabled = True
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


def test_needs_ack_sticky_scope_cannot_remove(app, ids):
    """needs_ack is model-level: no scope rule can downgrade it to plain allowed."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _set_needs_ack(app, model_id)
        # An explicit entity allow does not strip needs_ack.
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


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


def test_get_pool_limit_global_token_defaults_fallback(app, ids):
    """With no entity or group limit, get_pool_limit falls back to global TOKEN_DEFAULTS."""
    entity_id, _ = ids
    with app.app_context():
        old = app.config.get("TOKEN_DEFAULTS")
        app.config["TOKEN_DEFAULTS"] = {"max": 250, "refresh": 25, "starting": 250}
        try:
            assert get_pool_limit(entity_id) == (250.0, 25.0, 250.0)
        finally:
            app.config["TOKEN_DEFAULTS"] = old


# ---------------------------------------------------------------------------
# Entity-level model_access_default (used for project/service entities via yaml)
# Covers the fallthrough after no user or group rules.
# ---------------------------------------------------------------------------

def test_entity_model_access_default_blacklist(app, ids):
    """Entity model_access_default=blocked blocks when no user/group rules exist and the model has no own access."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.model_config import ModelConfig
        # Defaults apply only when the model does not pin its own access.
        db.session.get(ModelConfig, model_id).access = None
        entity = db.session.get(Entity, entity_id)
        entity.model_access_default = "blocked"
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_entity_model_access_default_needs_ack(app, ids):
    """Entity default allowed on a needs_ack model resolves to needs_ack."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        _set_needs_ack(app, model_id)
        entity = db.session.get(Entity, entity_id)
        entity.model_access_default = "allowed"
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


def test_entity_model_access_default_whitelist(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.get(Entity, entity_id)
        entity.model_access_default = "allowed"
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


# ---------------------------------------------------------------------------
# require_consent=False bypasses the consent gate for needs_ack models
# ---------------------------------------------------------------------------

def test_needs_ack_allows_access_when_require_consent_false(app, ids):
    """require_consent=False skips the consent DB check — needs_ack treated as allowed."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access(entity_id, model_id, require_consent=False) is True


def test_needs_ack_still_blocked_when_require_consent_true(app, ids):
    """require_consent=True (default) still gates on consent for needs_ack models."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="allowed"))
        db.session.commit()
        assert get_model_access(entity_id, model_id, require_consent=True) is False


def test_blocked_model_still_blocked_when_require_consent_false(app, ids):
    """require_consent=False never overrides a hard block."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blocked"))
        db.session.commit()
        assert get_model_access(entity_id, model_id, require_consent=False) is False
