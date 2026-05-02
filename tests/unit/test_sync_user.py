"""Tests for sync_user_from_yaml in auth routes."""
import pytest

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

        member = GroupMember.query.filter_by(entity_id=user).first()
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

        members = GroupMember.query.filter_by(entity_id=user).all()
        group_ids = {m.group_id for m in members}
        staff = Group.query.filter_by(name="staff").first()
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

        limit = EntityLimit.query.filter_by(entity_id=user).first()
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
        member = GroupMember.query.filter_by(entity_id=user, group_id=g2.id).first()
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

        uiuc_group = Group.query.filter_by(name="uiuc").first()
        member = GroupMember.query.filter_by(entity_id=user, group_id=uiuc_group.id).first()
        assert member is not None
