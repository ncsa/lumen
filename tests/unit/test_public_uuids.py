"""Opaque public uuids: every addressable row gets its own, and the lookup
helpers resolve one to its row or 404.

The uuid comes from the column's Python-side default, so it is set for any
ORM or Core insert, whichever route or command creates the row.
"""
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.conversation import Conversation
from lumen.models.entity import Entity
from lumen.models.group import Group
from lumen.models.lookups import (
    conversation_by_uuid_or_404,
    entity_by_uuid_or_404,
    group_by_uuid_or_404,
    key_by_uuid_or_404,
)
from lumen.services.erasure import get_or_create_ghost
from lumen.timeutils import utcnow
from tests.conftest import make_project

COMMENT = (
    "Opaque public identifier for external use only (URLs, API payloads, CLI, session); "
    "never used as a key or join column"
)
MODELS = (Entity, Group, APIKey, Conversation, AuthRequest)


def _user(email):
    user = Entity(entity_type="user", email=email, name=email, initials="U", active=True)
    db.session.add(user)
    db.session.commit()
    return user


def _rows():
    """Two of every kind of addressable row, created the way the app creates them."""
    users = [_user("a@example.com"), _user("b@example.com")]
    projects = [make_project("Alpha", owner_id=users[0].id), make_project("Beta", owner_id=users[0].id)]
    ghost = get_or_create_ghost()
    groups = [Group(name="g1"), Group(name="g2")]
    keys = [APIKey(entity_id=users[0].id, name=n, key_hash=n * 8) for n in ("k1", "k2")]
    conversations = [Conversation(entity_id=users[0].id, title=t) for t in ("c1", "c2")]
    requests = [
        AuthRequest(flow="device", client_id="cli", requested_name=n,
                    expires_at=utcnow() + timedelta(minutes=5))
        for n in ("r1", "r2")
    ]
    db.session.add_all(groups + keys + conversations + requests)
    db.session.commit()
    return users + projects + [ghost] + groups + keys + conversations + requests


def test_every_new_row_gets_a_distinct_uuid(app):
    with app.app_context():
        rows = _rows()
        db.session.expire_all()
        uuids = [row.uuid for row in rows]
        assert all(isinstance(u, UUID) for u in uuids)
        assert len(set(uuids)) == len(uuids)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
def test_uuid_column_is_unique_not_null_with_external_use_comment(model):
    column = model.__table__.c.uuid
    assert column.nullable is False
    assert column.comment == COMMENT
    indexes = [i for i in model.__table__.indexes if list(i.columns) == [column]]
    assert [(i.name, i.unique) for i in indexes] == [(f"ix_{model.__tablename__}_uuid", True)]


@pytest.fixture
def rows(app):
    with app.app_context():
        user = _user("owner@example.com")
        project = make_project("Lookup", owner_id=user.id)
        group = Group(name="lookup-group")
        key = APIKey(entity_id=user.id, name="lookup-key", key_hash="f" * 64)
        conversation = Conversation(entity_id=user.id, title="lookup")
        db.session.add_all([group, key, conversation])
        db.session.commit()
        return {
            "user": (user.id, user.uuid),
            "project": (project.id, project.uuid),
            "group": (group.id, group.uuid),
            "key": (key.id, key.uuid),
            "conversation": (conversation.id, conversation.uuid),
        }


def test_entity_by_uuid_finds_users_and_projects(app, rows):
    with app.test_request_context():
        assert entity_by_uuid_or_404(rows["user"][1], "user").id == rows["user"][0]
        assert entity_by_uuid_or_404(rows["project"][1], "project").id == rows["project"][0]


def test_entity_by_uuid_404s_for_the_wrong_entity_type(app, rows):
    with app.test_request_context():
        with pytest.raises(NotFound):
            entity_by_uuid_or_404(rows["user"][1], "project")
        with pytest.raises(NotFound):
            entity_by_uuid_or_404(rows["project"][1], "user")


def test_entity_by_uuid_finds_the_ghost(app):
    with app.app_context():
        ghost = get_or_create_ghost()
        ghost_id, ghost_uuid = ghost.id, ghost.uuid
    with app.test_request_context():
        assert entity_by_uuid_or_404(ghost_uuid, "ghost").id == ghost_id


@pytest.mark.parametrize("name, helper", [
    ("group", group_by_uuid_or_404),
    ("key", key_by_uuid_or_404),
    ("conversation", conversation_by_uuid_or_404),
])
def test_lookup_finds_its_row(app, rows, name, helper):
    row_id, row_uuid = rows[name]
    with app.test_request_context():
        assert helper(row_uuid).id == row_id


@pytest.mark.parametrize("helper", [
    lambda u: entity_by_uuid_or_404(u, "user"),
    lambda u: entity_by_uuid_or_404(u, "project"),
    group_by_uuid_or_404,
    key_by_uuid_or_404,
    conversation_by_uuid_or_404,
], ids=["user", "project", "group", "key", "conversation"])
def test_lookup_404s_for_an_unknown_uuid(app, rows, helper):
    with app.test_request_context():
        with pytest.raises(NotFound):
            helper(uuid4())


@pytest.mark.parametrize("name, helper", [
    ("user", group_by_uuid_or_404),
    ("group", key_by_uuid_or_404),
    ("key", conversation_by_uuid_or_404),
    ("conversation", lambda u: entity_by_uuid_or_404(u, "user")),
])
def test_lookup_404s_for_another_tables_uuid(app, rows, name, helper):
    """A uuid is only resolved in its own table, never matched across tables."""
    with app.test_request_context():
        with pytest.raises(NotFound):
            helper(rows[name][1])


def test_uuid_is_not_the_integer_id(app, rows):
    with app.app_context():
        found = db.session.execute(select(Entity.uuid).where(Entity.id == rows["user"][0])).scalar_one()
        assert found == rows["user"][1]
        assert str(found) != str(rows["user"][0])
