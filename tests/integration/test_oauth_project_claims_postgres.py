"""Concurrent OAuth project-key claims on PostgreSQL.

``_mint`` locks the approver's membership row (``SELECT ... FOR UPDATE``)
before the one-key check, so two overlapping claims by a ``user``-role member
serialize and only one key is minted. The lock is a no-op on SQLite, so this
is only provable here, with the claims in separate transactions.
"""
import os
import threading
import time
import uuid
from http import HTTPStatus

import pytest
from sqlalchemy import create_engine, func, select, text

from tests.conftest import entity_uuid, no_snapshot_refresher

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


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

    with pytest.MonkeyPatch.context() as mp:
        no_snapshot_refresher(mp)
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


def _project_with_user_member(app):
    """A project with a ``user``-role member; returns (project id, project name, member id)."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_manager import EntityManager

    with app.app_context():
        owner, member = (
            Entity(entity_type="user", email=f"{uuid.uuid4().hex[:10]}@example.invalid",
                   name="User", initials="US", active=True)
            for _ in range(2)
        )
        db.session.add_all([owner, member])
        db.session.flush()
        project = Entity(entity_type="project", name=f"claims-{uuid.uuid4().hex[:10]}",
                         initials="CL", active=True, owner_entity_id=owner.id)
        db.session.add(project)
        db.session.flush()
        db.session.add_all([
            EntityManager(user_entity_id=owner.id, project_entity_id=project.id),
            EntityManager(user_entity_id=member.id, project_entity_id=project.id, role="user"),
        ])
        db.session.commit()
        return project.id, project.name, member.id


def _approved_request(app, approver, project_name, key_name):
    """Issue a device request for the project and approve it; returns the device_code response."""
    from lumen.extensions import db
    from lumen.models.auth_request import AuthRequest

    body = app.test_client().post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": key_name, "project": project_name,
    }).get_json()
    assert approver.get(f"/device?code={body['user_code']}").status_code == HTTPStatus.OK
    with app.app_context():
        request_id = db.session.execute(
            select(AuthRequest.uuid).where(AuthRequest.user_code == body["user_code"])
        ).scalar_one()
    resp = approver.post("/oauth/consent", data={"request_id": str(request_id), "action": "approve"})
    assert resp.status_code == HTTPStatus.OK and b"Key approved" in resp.data
    return body


def test_overlapping_claims_by_user_role_member_mint_one_key(pg_app, pg_migrated_isolated):
    """Both claims block on the membership lock, then run one after the other:
    the first mints, the second sees that key and is refused."""
    from lumen.extensions import db
    from lumen.models.api_key import APIKey

    url, engine = pg_migrated_isolated
    project_id, project_name, member_id = _project_with_user_member(pg_app)
    approver = pg_app.test_client()
    with approver.session_transaction() as sess:
        sess["entity_uuid"] = entity_uuid(pg_app, member_id)
    bodies = [_approved_request(pg_app, approver, project_name, name) for name in ("one", "two")]

    results = [None, None]

    def claim(i):
        results[i] = pg_app.test_client().post("/oauth/token", data={
            "grant_type": DEVICE_GRANT, "device_code": bodies[i]["device_code"], "client_id": "lumen-cli",
        })

    other = create_engine(url)
    try:
        with other.connect() as conn:
            # Hold the member's row so both claims reach the lock before either can pass it.
            conn.execute(text(
                "SELECT id FROM entity_managers WHERE user_entity_id = :m AND project_entity_id = :p FOR UPDATE"
            ), {"m": member_id, "p": project_id})
            threads = [threading.Thread(target=claim, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            with engine.connect() as probe:
                deadline = time.monotonic() + 30
                while probe.execute(text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )).scalar() < 2:
                    assert time.monotonic() < deadline, "the claims never both blocked on the membership lock"
                    time.sleep(0.05)
            conn.rollback()
            for thread in threads:
                thread.join(timeout=30)
    finally:
        other.dispose()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [HTTPStatus.OK, HTTPStatus.BAD_REQUEST]
    refused = next(r for r in results if r.status_code == HTTPStatus.BAD_REQUEST).get_json()
    assert refused == {
        "error": "access_denied",
        "error_description": (
            f"members with the user role can hold one key in project '{project_name}'; "
            "request it with the name of your existing key to replace it"
        ),
    }
    with pg_app.app_context():
        db.session.expire_all()
        assert db.session.scalar(
            select(func.count(APIKey.id)).where(APIKey.entity_id == project_id, APIKey.revoked_at.is_(None))
        ) == 1
