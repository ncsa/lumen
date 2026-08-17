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


def test_sync_adds_extra_group(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="default", active=True, config_managed=True))
        db.session.add(Group(name="staff", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        sync_user_from_yaml(entity, "sync@example.com", {}, extra_groups=["staff"])
        db.session.commit()

        members = db.session.execute(select(GroupMember).filter_by(entity_id=user)).scalars().all()
        group_ids = {m.group_id for m in members}
        staff = db.session.execute(select(Group).filter_by(name="staff")).scalar_one_or_none()
        assert staff.id in group_ids


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
            "group_rules": {
                "uiuc": [{"field": "eppn", "contains": "@illinois.edu"}]
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
            "group_rules": {
                "uiuc": [{"field": "eppn", "contains": "@illinois.edu"}]
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
            "group_rules": {
                "staff": [{"field": "affiliation", "equals": "staff"}]
            }
        }
        userinfo = {"affiliation": "staff"}
        sync_user_from_yaml(entity, "sync@example.com", yaml_data, userinfo=userinfo)
        db.session.commit()

        staff_group = db.session.execute(select(Group).filter_by(name="staff")).scalar_one_or_none()
        member = db.session.execute(select(GroupMember).filter_by(entity_id=user, group_id=staff_group.id)).scalar_one_or_none()
        assert member is not None


def test_sync_ignores_users_section(app, user):
    """The removed users: section has no effect on memberships (config is v3)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add(Group(name="staff", active=True, config_managed=True))
        db.session.commit()

        entity = db.session.get(Entity, user)
        yaml_data = {"users": {"sync@example.com": {"groups": ["staff"]}}}
        sync_user_from_yaml(entity, "sync@example.com", yaml_data)
        db.session.commit()

        staff = db.session.execute(select(Group).filter_by(name="staff")).scalar_one()
        member = db.session.execute(
            select(GroupMember).filter_by(entity_id=user, group_id=staff.id)
        ).scalar_one_or_none()
        assert member is None


def test_sync_null_users_entry_is_harmless(app, user):
    """A users: entry with a null value must not crash login sync."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        entity = db.session.get(Entity, user)
        sync_user_from_yaml(entity, "sync@example.com", {"users": {"sync@example.com": None}})
        db.session.commit()


def _rules_sync(app, user, group_rules, userinfo):
    """Run sync_user_from_yaml with the given group_rules and userinfo; return the user's group ids."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.group_member import GroupMember
    entity = db.session.get(Entity, user)
    sync_user_from_yaml(entity, "sync@example.com", {"group_rules": group_rules}, userinfo=userinfo)
    db.session.commit()
    return {m.group_id for m in db.session.execute(
        select(GroupMember).filter_by(entity_id=user)).scalars().all()}


def test_sync_rule_without_field_fails_closed(app, user):
    """A rule with no 'field' key must never match — not silently match everyone."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="broken", active=True, config_managed=True)
        db.session.add(g)
        db.session.commit()
        assert g.id not in _rules_sync(app, user, {"broken": [{}]}, {"eppn": "x@y.edu"})
        assert g.id not in _rules_sync(app, user, {"broken": [{"contains": "y.edu"}]}, {"eppn": "x@y.edu"})


def test_sync_mixed_fieldless_rule_blocks_group(app, user):
    """All rules must match; one malformed rule in the list blocks the assignment."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="mixed", active=True, config_managed=True)
        db.session.add(g)
        db.session.commit()
        rules = [{"field": "eppn", "contains": "@y.edu"}, {}]
        assert g.id not in _rules_sync(app, user, {"mixed": rules}, {"eppn": "x@y.edu"})


def test_sync_rule_without_matcher_fails_closed(app, user):
    """A rule with a field but neither contains nor equals must not match."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="nomatcher", active=True, config_managed=True)
        db.session.add(g)
        db.session.commit()
        # Previously this compared the field value to "" and matched users
        # who LACK the field entirely.
        assert g.id not in _rules_sync(app, user, {"nomatcher": [{"field": "absent"}]}, {"eppn": "x@y.edu"})
