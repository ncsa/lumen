"""Project ownership (``entities.owner_entity_id``) as the migration chain builds it.

SQLite enforces the CHECK but not foreign keys, so the composite, deferred
``fk_entities_owner_membership`` — the owner is a manager, and their manager
row cannot be deleted — and the row locking behind concurrent ownership
transfers are only provable here, on a database the real chain migrated.
"""
import os
import threading
import time
import uuid
from http import HTTPStatus

import pytest
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.exc import IntegrityError

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A real Flask app bound to this module's migrated database."""
    url = pg_migrated_isolated[0]
    previous_env = {k: os.environ.get(k) for k in ("DATABASE_URL", "CONFIG_YAML", "BACKGROUND_WORKER")}
    os.environ.update({"DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"})
    from config import Config

    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app

    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        from lumen.extensions import db

        assert db.engine.dialect.name == "postgresql", "the test app is not on PostgreSQL"
    yield application
    for key, value in previous_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _user(email=None, **fields):
    from lumen.extensions import db
    from lumen.models.entity import Entity

    user = Entity(entity_type="user", email=email or f"{uuid.uuid4().hex[:10]}@example.invalid",
                  name=fields.pop("name", "User"), initials="US", active=True, **fields)
    db.session.add(user)
    db.session.flush()
    return user


def _project(owner, *managers):
    """A project owned by ``owner``, with ``managers`` as extra manager rows; commits."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_manager import EntityManager

    project = Entity(entity_type="project", name=f"proj-{uuid.uuid4().hex[:10]}",
                     initials="PR", active=True, owner_entity_id=owner.id)
    db.session.add(project)
    db.session.flush()
    for user in (owner, *managers):
        db.session.add(EntityManager(user_entity_id=user.id, project_entity_id=project.id))
    db.session.commit()
    return project


def _client_as(app, user_id, admin=False):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = user_id
        sess["entity_name"] = "User"
        sess["initials"] = "US"
        sess["gravatar_hash"] = ""
        if admin:
            sess["admin_mode"] = True
    return client


def test_migrated_owner_column_is_commented(pg_app):
    from lumen.extensions import db
    from lumen.models.entity import Entity

    with pg_app.app_context():
        comment = db.session.execute(text("""
            SELECT col_description('entities'::regclass, attnum)
            FROM pg_attribute
            WHERE attrelid = 'entities'::regclass AND attname = 'owner_entity_id'
        """)).scalar()
        assert comment == Entity.__table__.columns["owner_entity_id"].comment


def test_project_without_owner_fails_check(pg_app):
    from lumen.extensions import db
    from lumen.models.entity import Entity

    with pg_app.app_context():
        db.session.add(Entity(entity_type="project", name="no-owner", initials="NO", active=True))
        with pytest.raises(IntegrityError, match="ck_entities_project_owner"):
            db.session.commit()
        db.session.rollback()


def test_deleting_owner_membership_fails_fk(pg_app):
    from lumen.extensions import db
    from lumen.models.entity_manager import EntityManager

    with pg_app.app_context():
        owner = _user()
        project = _project(owner)
        # ON DELETE RESTRICT is never deferred: the DELETE itself fails.
        with pytest.raises(IntegrityError, match="fk_entities_owner_membership"):
            db.session.execute(delete(EntityManager).where(
                EntityManager.user_entity_id == owner.id,
                EntityManager.project_entity_id == project.id,
            ))
        db.session.rollback()


def test_owner_pointing_at_non_member_fails_fk(pg_app):
    from lumen.extensions import db
    from lumen.models.entity import Entity

    with pg_app.app_context():
        owner = _user()
        outsider = _user()
        project = _project(owner)
        db.session.execute(update(Entity).where(Entity.id == project.id).values(owner_entity_id=outsider.id))
        with pytest.raises(IntegrityError, match="fk_entities_owner_membership"):
            db.session.commit()
        db.session.rollback()


def test_create_project_route_inserts_project_and_owner_in_one_transaction(pg_app):
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_manager import EntityManager

    with pg_app.app_context():
        admin = db.session.execute(select(Entity).filter_by(email="admin@example.com")).scalar_one_or_none()
        admin = admin or _user("admin@example.com", name="Admin")
        owner = _user()
        db.session.commit()
        admin_id, owner_id, owner_email = admin.id, owner.id, owner.email

    resp = _client_as(pg_app, admin_id, admin=True).post(
        "/projects", json={"name": f"pg-created-{uuid.uuid4().hex[:8]}", "owner_email": owner_email},
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.get_data(as_text=True)

    with pg_app.app_context():
        project = db.session.get(Entity, resp.get_json()["id"])
        assert project.owner_entity_id == owner_id
        assert db.session.execute(select(EntityManager).filter_by(
            user_entity_id=owner_id, project_entity_id=project.id,
        )).scalar_one_or_none() is not None


def test_concurrent_transfers_one_succeeds_one_conflicts(pg_app, pg_migrated_isolated):
    """Transfer 1 holds the project row; transfer 2 (the route) blocks on it,
    then finds the owner it read is gone and answers 409."""
    from lumen.extensions import db
    from lumen.models.entity import Entity

    url, engine = pg_migrated_isolated
    with pg_app.app_context():
        owner = _user()
        first, second = _user(), _user()
        project = _project(owner, first, second)
        sid, owner_id, first_id, second_id = project.id, owner.id, first.id, second.id

    other = create_engine(url)
    try:
        with other.connect() as conn:
            moved = conn.execute(text(
                "UPDATE entities SET owner_entity_id = :new WHERE id = :sid AND owner_entity_id = :old"
            ), {"new": first_id, "sid": sid, "old": owner_id}).rowcount
            assert moved == 1

            result = {}

            def transfer():
                client = _client_as(pg_app, owner_id)
                result["resp"] = client.post(f"/projects/{sid}/owner", json={"user_id": second_id})

            thread = threading.Thread(target=transfer)
            thread.start()
            # Wait until the route's UPDATE is blocked on transfer 1's row lock.
            with engine.connect() as probe:
                deadline = time.monotonic() + 30
                while not probe.execute(text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )).scalar():
                    assert time.monotonic() < deadline, "the second transfer never blocked"
                    time.sleep(0.05)
            conn.commit()
            thread.join(timeout=30)
    finally:
        other.dispose()

    resp = result["resp"]
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "concurrently" in resp.get_json()["error"]
    with pg_app.app_context():
        db.session.expire_all()
        assert db.session.get(Entity, sid).owner_entity_id == first_id
