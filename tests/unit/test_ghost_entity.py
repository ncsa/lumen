"""The shared ghost entity for GDPR erasure: creation, uniqueness, and that it
stays out of every place a user can be found or log in.

Each listing check has an active user named "Erased Smith" as a control, so a
search for "Erased" is shown to match and only the ghost is left out.
"""
from http import HTTPStatus

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.services.erasure import get_or_create_ghost
from tests.conftest import make_project


@pytest.fixture
def ghost_id(app):
    with app.app_context():
        return get_or_create_ghost().id


@pytest.fixture
def control_user(app):
    """An active user whose name also matches a search for 'Erased'."""
    with app.app_context():
        user = Entity(entity_type="user", email="smith@example.com", name="Erased Smith",
                      initials="ES", active=True)
        db.session.add(user)
        db.session.commit()
        return user.id


def _ghost_count():
    return db.session.execute(
        select(func.count()).select_from(Entity).where(Entity.entity_type == "ghost")
    ).scalar_one()


def test_get_or_create_ghost_creates_one_anonymous_inactive_ghost(app):
    with app.app_context():
        ghost = get_or_create_ghost()
        assert ghost.entity_type == "ghost"
        assert ghost.email is None
        assert ghost.active is False
        assert ghost.name == "Erased user"
        assert ghost.owner_entity_id is None
        assert ghost.entity_balance is None
        assert ghost.entity_limit is None
        assert ghost.api_keys == []


def test_get_or_create_ghost_is_idempotent(app):
    with app.app_context():
        first = get_or_create_ghost().id
        db.session.expire_all()
        assert get_or_create_ghost().id == first
        assert _ghost_count() == 1


def test_second_ghost_insert_violates_unique_index(app, ghost_id):
    with app.app_context():
        db.session.add(Entity(entity_type="ghost", name="Another ghost", initials="", active=False))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert _ghost_count() == 1


def test_get_or_create_ghost_rereads_after_losing_the_race(app, monkeypatch):
    """Another process creates the ghost between our lookup and our insert."""
    with app.app_context():
        real_add = db.session.add

        def add_after_rival(entity):
            with db.engine.begin() as conn:
                conn.execute(Entity.__table__.insert().values(
                    entity_type="ghost", name="Erased user", initials="",
                    active=False, store_conversations=True,
                ))
            real_add(entity)

        monkeypatch.setattr(db.session, "add", add_after_rival)
        ghost = get_or_create_ghost()
        monkeypatch.undo()

        rival = db.session.execute(
            select(Entity.id).where(Entity.entity_type == "ghost")
        ).scalar_one()
        assert ghost.id == rival
        assert _ghost_count() == 1


def test_ghost_absent_from_admin_users_list(admin_client, ghost_id, control_user):
    ids = [u["id"] for u in admin_client.get("/admin/api/users").get_json()["users"]]
    assert control_user in ids
    assert ghost_id not in ids

    ids = [u["id"] for u in admin_client.get("/admin/api/users?search=Erased").get_json()["users"]]
    assert ids == [control_user]


def test_ghost_absent_from_admin_typeahead(admin_client, ghost_id, control_user):
    users = admin_client.get("/admin/api/users/search?q=Erased").get_json()["users"]
    assert [u["name"] for u in users] == ["Erased Smith"]


def test_admin_user_edit_404s_for_ghost(admin_client, ghost_id):
    resp = admin_client.patch(f"/admin/users/{ghost_id}", json={"active": True})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_ghost_absent_from_group_member_search(app, auth_client, test_user, ghost_id, control_user):
    with app.app_context():
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        group = Group(name="owned-group", active=True)
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(group_id=group.id, entity_id=test_user["id"], is_owner=True))
        db.session.commit()
        gid = group.id

    entities = auth_client.get(f"/groups/{gid}/members/search?q=Erased").get_json()["entities"]
    assert [e["id"] for e in entities] == [control_user]


def test_ghost_absent_from_project_member_search(app, auth_client, test_user, ghost_id, control_user):
    with app.app_context():
        pid = make_project("Lab", owner_id=test_user["id"]).id

    users = auth_client.get(f"/projects/{pid}/users/search?q=Erased").get_json()["users"]
    assert [u["id"] for u in users] == [control_user]


def test_login_required_redirects_a_ghost_session(client, ghost_id):
    with client.session_transaction() as sess:
        sess["entity_id"] = ghost_id
    resp = client.get("/profile")
    assert resp.status_code == HTTPStatus.FOUND
    with client.session_transaction() as sess:
        assert "entity_id" not in sess
