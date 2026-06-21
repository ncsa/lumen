"""Tests for sync_user_from_yaml in auth routes."""
import pytest
from sqlalchemy import select

from lumen.blueprints.auth.routes import sync_user_from_yaml


@pytest.fixture
def user(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        e = Entity(entity_type="user", email="sync@example.com", name="Sync User", initials="SU", active=True)
        db.session.add(e)
        db.session.commit()
        db.session.refresh(e)
        return e.id


def test_sync_adds_default_group(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="default", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        sync_user_from_yaml(entity, "sync@example.com", {"groups": {"default": {}}})
        db.session.commit()

        member = db.session.execute(select(GroupMember).filter_by(entity_id=user)).scalar_one_or_none()
        assert member is not None


def test_sync_adds_named_group(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="default", active=True, config_managed=True))
        db.session.add(Group(name="staff", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {
            "users": {"sync@example.com": {"groups": ["staff"]}},
            "groups": {"default": {}, "staff": {}},
        }
        sync_user_from_yaml(entity, "sync@example.com", yaml_data)
        db.session.commit()

        members = db.session.execute(select(GroupMember).filter_by(entity_id=user)).scalars().all()
        group_ids = {m.group_id for m in members}
        staff = db.session.execute(select(Group).filter_by(name="staff")).scalar_one_or_none()
        assert staff.id in group_ids


def test_sync_sets_entity_limit(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_limit import EntityLimit
        entity = db.session.get(Entity, user)
        yaml_data = {
            "users": {
                "sync@example.com": {
                    "pool": {"max": 100, "refresh": 10, "starting": 100}
                }
            }
        }
        sync_user_from_yaml(entity, "sync@example.com", yaml_data)
        db.session.commit()

        limit = db.session.execute(select(EntityLimit).filter_by(entity_id=user)).scalar_one_or_none()
        assert limit is not None
        assert float(limit.max_coins) == 100.0
        assert float(limit.refresh_coins) == 10.0


def test_sync_removes_stale_group(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        g = Group(name="default", active=True, config_managed=True)
        g2 = Group(name="old-group", active=True, config_managed=True)
        db.session.add_all([g, g2])
        db.session.commit()
        # Manually add a config-managed membership to old-group
        db.session.add(GroupMember(group_id=g2.id, entity_id=user, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        # Sync with only default group desired
        sync_user_from_yaml(entity, "sync@example.com", {})
        db.session.commit()

        # old-group membership should have been removed
        member = db.session.execute(select(GroupMember).filter_by(entity_id=user, group_id=g2.id)).scalar_one_or_none()
        assert member is None


def test_sync_rule_based_group_assignment(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="default", active=True, config_managed=True))
        db.session.add(Group(name="uiuc", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {
            "groups": {
                "uiuc": {
                    "rules": [
                        {"field": "eppn", "contains": "@illinois.edu"}
                    ]
                }
            }
        }
        userinfo = {"eppn": "testuser@illinois.edu"}
        sync_user_from_yaml(entity, "sync@example.com", yaml_data, userinfo=userinfo)
        db.session.commit()

        uiuc_group = db.session.execute(select(Group).filter_by(name="uiuc")).scalar_one_or_none()
        member = db.session.execute(select(GroupMember).filter_by(entity_id=user, group_id=uiuc_group.id)).scalar_one_or_none()
        assert member is not None


def test_sync_rule_no_match_does_not_assign_group(app, user):
    """Rule present but field value doesn't match → user NOT added to the group."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="uiuc", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {
            "groups": {
                "uiuc": {
                    "rules": [{"field": "eppn", "contains": "@illinois.edu"}]
                }
            }
        }
        userinfo = {"eppn": "testuser@other.edu"}  # doesn't contain @illinois.edu
        sync_user_from_yaml(entity, "sync@example.com", yaml_data, userinfo=userinfo)
        db.session.commit()

        uiuc_group = db.session.execute(select(Group).filter_by(name="uiuc")).scalar_one_or_none()
        member = db.session.execute(select(GroupMember).filter_by(entity_id=user, group_id=uiuc_group.id)).scalar_one_or_none()
        assert member is None


def test_sync_rule_equals_type(app, user):
    """Rule with 'equals' predicate assigns the group when the field matches exactly."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="staff", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {
            "groups": {
                "staff": {
                    "rules": [{"field": "affiliation", "equals": "staff"}]
                }
            }
        }
        userinfo = {"affiliation": "staff"}
        sync_user_from_yaml(entity, "sync@example.com", yaml_data, userinfo=userinfo)
        db.session.commit()

        staff_group = db.session.execute(select(Group).filter_by(name="staff")).scalar_one_or_none()
        member = db.session.execute(select(GroupMember).filter_by(entity_id=user, group_id=staff_group.id)).scalar_one_or_none()
        assert member is not None


def test_sync_removes_entity_limit_when_pool_removed_from_yaml(app, user):
    """If pool config is removed from yaml, any config-managed EntityLimit is deleted (else branch)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_limit import EntityLimit
        entity = db.session.get(Entity, user)
        # First sync: create a config-managed limit.
        sync_user_from_yaml(entity, "sync@example.com", {
            "users": {"sync@example.com": {"pool": {"max": 100, "refresh": 10, "starting": 100}}}
        })
        db.session.commit()
        assert db.session.execute(select(EntityLimit).filter_by(entity_id=user)).scalar_one_or_none() is not None

        # Second sync: no pool config → the config-managed limit should be deleted.
        entity = db.session.get(Entity, user)
        sync_user_from_yaml(entity, "sync@example.com", {})
        db.session.commit()
        assert db.session.execute(select(EntityLimit).filter_by(entity_id=user)).scalar_one_or_none() is None


def test_sync_user_model_whitelist(app, user):
    """users.<email>.models list whitelists specific models for the user."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(model_name="allowed-model", input_cost_per_million=1.0, output_cost_per_million=1.0, access="allowed")
        db.session.add(mc)
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {
            "users": {"sync@example.com": {"models": ["allowed-model"]}}
        }
        sync_user_from_yaml(entity, "sync@example.com", yaml_data)
        db.session.commit()

        rule = db.session.execute(
            select(EntityModelAccess).filter_by(entity_id=user, model_config_id=mc.id)
        ).scalar_one_or_none()
        assert rule is not None
        assert rule.access_type == "allowed"


def test_sync_user_model_access_allowed_blocked_default(app, user):
    """users.<email>.model_access sets allowed/blocked rows and the entity default."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.model_config import ModelConfig
        a = ModelConfig(model_name="a-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
        b = ModelConfig(model_name="b-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add_all([a, b])
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {"users": {"sync@example.com": {"model_access": {
            "default": "blocked", "allowed": ["a-model"], "blocked": ["b-model"],
        }}}}
        sync_user_from_yaml(entity, "sync@example.com", yaml_data)
        db.session.commit()

        entity = db.session.get(Entity, user)
        assert entity.model_access_default == "blocked"
        rows = {
            r.model_config_id: r.access_type
            for r in db.session.execute(select(EntityModelAccess).filter_by(entity_id=user)).scalars().all()
        }
        assert rows == {a.id: "allowed", b.id: "blocked"}


def test_sync_user_model_access_replaces_previous(app, user):
    """Re-syncing with a different model_access removes stale per-user rows."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.model_config import ModelConfig
        a = ModelConfig(model_name="a-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add(a)
        db.session.commit()
        entity = db.session.get(Entity, user)

        sync_user_from_yaml(entity, "sync@example.com", {"users": {"sync@example.com": {"model_access": {"blocked": ["a-model"]}}}})
        db.session.commit()
        # Now drop the rule entirely.
        sync_user_from_yaml(entity, "sync@example.com", {"users": {"sync@example.com": {}}})
        db.session.commit()
        assert db.session.execute(select(EntityModelAccess).filter_by(entity_id=user)).scalars().all() == []
