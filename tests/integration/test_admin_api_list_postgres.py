"""The admin API user list on PostgreSQL.

Runs ``list_pages_through_every_sort`` and ``search_is_literal`` from
``tests/routes/test_admin_api.py``: the coalesced sort keys, the ``(sort value,
entity id)`` cursor, the ties on ``created_at`` and ``last_active`` and the
literal ``search`` have to behave identically on both backends.
"""

import uuid
from http import HTTPStatus

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_ADMIN, APIKey
from lumen.models.entity import Entity
from lumen.services.crypto import hash_api_key
from tests.integration.conftest import pg_app as make_pg_app
from tests.routes.test_admin_api import list_pages_through_every_sort, search_is_literal

pytestmark = pytest.mark.postgres

ADMIN_EMAIL = "admin@example.com"  # in the test config's admins list


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A Flask app bound to this module's migrated PostgreSQL database."""
    with make_pg_app(pg_migrated_isolated[0]) as application:
        application.config["TESTING"] = True
        with application.app_context():
            assert db.engine.dialect.name == "postgresql"
        yield application


@pytest.fixture
def admin_token(pg_app):
    """A fresh admin key owned by the configured admin."""
    token = f"sk_admin_pg_list_{uuid.uuid4().hex}"
    with pg_app.app_context():
        admin = db.session.execute(select(Entity).where(Entity.email == ADMIN_EMAIL)).scalar_one_or_none()
        if admin is None:
            admin = Entity(entity_type="user", email=ADMIN_EMAIL, name="Admin User", active=True)
            db.session.add(admin)
            db.session.flush()
        db.session.add(APIKey(entity_id=admin.id, name="pg list", key_type=KEY_TYPE_ADMIN,
                              key_hash=hash_api_key(token)))
        db.session.commit()
    return token


def test_list_pages_through_every_sort_on_postgres(pg_app, admin_token):
    list_pages_through_every_sort(pg_app, pg_app.test_client(), admin_token)


def test_search_is_literal_on_postgres(pg_app, admin_token):
    # PostgreSQL's LIKE treats a backslash as its default escape; SQLite does not.
    search_is_literal(pg_app, pg_app.test_client(), admin_token)


def test_search_with_nul_is_400_on_postgres(pg_app, admin_token):
    # psycopg rejects NUL in a text bind, so without the check this was a 500.
    resp = pg_app.test_client().get("/v1/organization/users", query_string={"search": "ada\x00"},
                                    headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.get_json()["error"]["param"] == "search"
