"""Tests for sync_auto_memberships (login group reconciliation) in auth routes."""
import pytest
from sqlalchemy import select

from lumen.blueprints.auth.routes import sync_auto_memberships


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


def _make_group(name, auto_join=False, rules=()):
    """Create a group with optional auto-join rules; returns the Group. Call inside an app context."""
    from lumen.extensions import db
    from lumen.models.group import Group
    from lumen.models.group_rule import GroupRule
    g = Group(name=name, active=True, auto_join=auto_join)
    db.session.add(g)
    db.session.flush()
    for field, match, value in rules:
        db.session.add(GroupRule(group_id=g.id, field=field, match=match, value=value))
    db.session.commit()
    return g


def _sync(app, user, userinfo=None, extra_groups=None):
    """Run sync_auto_memberships; return the user's group ids."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.group_member import GroupMember
    entity = db.session.get(Entity, user)
    sync_auto_memberships(entity, userinfo=userinfo, extra_groups=extra_groups)
    db.session.commit()
    return {m.group_id for m in db.session.execute(
        select(GroupMember).filter_by(entity_id=user)).scalars().all()}


def test_sync_group_named_default_is_not_special(app, user):
    """The implicit everyone-group is gone: a group named "default" is a
    perfectly ordinary group and nobody is auto-joined to it."""
    with app.app_context():
        g = _make_group("default")
        assert g.id not in _sync(app, user)


def test_sync_adds_extra_group(app, user):
    with app.app_context():
        staff = _make_group("staff")
        assert staff.id in _sync(app, user, extra_groups=["staff"])


def test_sync_ignores_missing_extra_group(app, user):
    """dev_user.groups naming a group that does not exist is ignored — never created."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        ids = _sync(app, user, extra_groups=["does-not-exist"])
        assert ids == set()
        assert db.session.execute(
            select(Group).filter_by(name="does-not-exist")
        ).scalar_one_or_none() is None


def test_sync_removes_stale_group(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        old = _make_group("old-group")
        db.session.add(GroupMember(group_id=old.id, entity_id=user, config_managed=True))
        db.session.commit()
        assert old.id not in _sync(app, user)


def test_sync_keeps_manual_membership(app, user):
    """Only auto-assigned (config_managed) memberships are reconciled away."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        manual = _make_group("hand-picked")
        db.session.add(GroupMember(group_id=manual.id, entity_id=user, config_managed=False))
        db.session.commit()
        assert manual.id in _sync(app, user)


def test_sync_rule_contains_match(app, user):
    with app.app_context():
        g = _make_group("uiuc", auto_join=True, rules=[("eppn", "contains", "@illinois.edu")])
        assert g.id in _sync(app, user, userinfo={"eppn": "testuser@illinois.edu"})


def test_sync_rule_no_match_does_not_assign(app, user):
    with app.app_context():
        g = _make_group("uiuc", auto_join=True, rules=[("eppn", "contains", "@illinois.edu")])
        assert g.id not in _sync(app, user, userinfo={"eppn": "testuser@other.edu"})


def test_sync_rule_equals_match(app, user):
    with app.app_context():
        g = _make_group("staff", auto_join=True, rules=[("affiliation", "equals", "staff")])
        assert g.id in _sync(app, user, userinfo={"affiliation": "staff"})
        # equals is exact, not substring
        g2 = _make_group("staff2", auto_join=True, rules=[("affiliation", "equals", "staff")])
        assert g2.id not in _sync(app, user, userinfo={"affiliation": "staff@x.edu"})


def test_sync_all_rules_must_match(app, user):
    with app.app_context():
        g = _make_group("both", auto_join=True, rules=[
            ("eppn", "contains", "@y.edu"),
            ("idp", "equals", "urn:example"),
        ])
        assert g.id not in _sync(app, user, userinfo={"eppn": "x@y.edu", "idp": "other"})
        assert g.id in _sync(app, user, userinfo={"eppn": "x@y.edu", "idp": "urn:example"})


def test_sync_auto_join_off_does_not_assign(app, user):
    """Rules stay dormant while auto_join is off."""
    with app.app_context():
        g = _make_group("dormant", auto_join=False, rules=[("eppn", "contains", "@y.edu")])
        assert g.id not in _sync(app, user, userinfo={"eppn": "x@y.edu"})


def test_sync_auto_join_without_rules_fails_closed(app, user):
    """auto_join with an empty rule set must never match everyone."""
    with app.app_context():
        g = _make_group("empty", auto_join=True)
        assert g.id not in _sync(app, user, userinfo={"eppn": "x@y.edu"})


def test_sync_removal_when_rules_stop_matching(app, user):
    """A rule-assigned membership is removed at the next login that no longer matches."""
    with app.app_context():
        g = _make_group("uiuc", auto_join=True, rules=[("eppn", "contains", "@illinois.edu")])
        assert g.id in _sync(app, user, userinfo={"eppn": "x@illinois.edu"})
        assert g.id not in _sync(app, user, userinfo={"eppn": "x@other.edu"})


def test_sync_inactive_auto_join_group_stops_matching(app, user):
    """Deactivating a group pauses its auto-join: no new members, and existing
    auto-memberships are removed at the next login."""
    with app.app_context():
        from lumen.extensions import db
        g = _make_group("paused", auto_join=True, rules=[("eppn", "contains", "@y.edu")])
        userinfo = {"eppn": "x@y.edu"}
        assert g.id in _sync(app, user, userinfo=userinfo)
        g.active = False
        db.session.commit()
        assert g.id not in _sync(app, user, userinfo=userinfo)
        # Reactivating resumes auto-join at the next login.
        g.active = True
        db.session.commit()
        assert g.id in _sync(app, user, userinfo=userinfo)


def test_sync_inactive_extra_groups_ignored(app, user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        db.session.add(Group(name="staff", active=False))
        db.session.commit()
        assert _sync(app, user, extra_groups=["staff"]) == set()


def test_sync_rule_on_non_string_claim_does_not_crash(app, user):
    """Provider claims are not always strings: a 'contains' rule on a boolean
    claim (email_verified) must not raise inside the login callback."""
    with app.app_context():
        g = _make_group("verified", auto_join=True, rules=[("email_verified", "contains", "true")])
        ids = _sync(app, user, userinfo={"email_verified": True, "eppn": "x@y.edu"})
        assert g.id not in ids  # str(True) == "True"; "true" is not a substring
        g2 = _make_group("verified2", auto_join=True, rules=[("email_verified", "equals", "True")])
        assert g2.id in _sync(app, user, userinfo={"email_verified": True})


def test_sync_rule_matches_list_claims(app, user):
    """CILogon's is_member_of is a list; contains/equals match any element."""
    with app.app_context():
        g = _make_group("aifarms", auto_join=True, rules=[("is_member_of", "contains", "grp-aifarms")])
        assert g.id in _sync(app, user, userinfo={"is_member_of": ["icc-grp-aifarms", "other"]})
        assert g.id not in _sync(app, user, userinfo={"is_member_of": ["unrelated"]})
