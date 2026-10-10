"""get_project_role: owner, manager, user, or None for a non-member."""
import pytest
import sqlalchemy as sa

from tests.conftest import make_project


def _make_user(email):
    from lumen.extensions import db
    from lumen.models.entity import Entity
    user = Entity(entity_type="user", email=email, name=email, initials="US", active=True)
    db.session.add(user)
    db.session.flush()
    return user.id


@pytest.fixture
def members(app):
    """A project with an owner, a manager, a user, and an unrelated outsider."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_manager import EntityManager
        project = make_project("role-svc")
        manager_id = _make_user("manager@example.com")
        user_id = _make_user("user@example.com")
        outsider_id = _make_user("outsider@example.com")
        db.session.add(EntityManager(user_entity_id=manager_id, project_entity_id=project.id))
        db.session.add(EntityManager(user_entity_id=user_id, project_entity_id=project.id, role="user"))
        db.session.commit()
        return {
            "project": project.id,
            "owner": project.owner_entity_id,
            "manager": manager_id,
            "user": user_id,
            "outsider": outsider_id,
        }


@pytest.mark.parametrize("who, expected", [
    ("owner", "owner"),
    ("manager", "manager"),
    ("user", "user"),
    ("outsider", None),
])
def test_get_project_role(app, members, who, expected):
    from lumen.models.entity_manager import get_project_role
    with app.app_context():
        assert get_project_role(members[who], members["project"]) == expected


def test_get_project_role_other_project_is_none(app, members):
    """Membership in one project says nothing about another."""
    from lumen.models.entity_manager import get_project_role
    with app.app_context():
        other = make_project("other-role-svc")
        assert get_project_role(members["manager"], other.id) is None
        assert get_project_role(members["owner"], other.id) is None


def test_new_member_defaults_to_manager(app, members):
    from lumen.extensions import db
    from lumen.models.entity_manager import EntityManager
    with app.app_context():
        row = db.session.execute(
            sa.select(EntityManager).filter_by(
                user_entity_id=members["owner"], project_entity_id=members["project"],
            )
        ).scalar_one()
        assert row.role == "manager"


def test_role_outside_allowed_set_rejected(app, members):
    from lumen.extensions import db
    from lumen.models.entity_manager import EntityManager
    with app.app_context():
        db.session.add(EntityManager(
            user_entity_id=members["outsider"], project_entity_id=members["project"], role="admin",
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.session.commit()
        db.session.rollback()
