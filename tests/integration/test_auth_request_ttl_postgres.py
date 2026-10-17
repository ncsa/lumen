"""``auth_requests.ttl`` as the migration chain builds it.

The SQLite suite builds the schema with ``create_all`` and never runs Alembic,
so leaving existing requests with a NULL ttl, the column comment, and room
for every TTL the resolver allows (past 32 bits, up to 100 years) are only
provable here, on a database the real chain migrated over existing rows.
"""
import os
import uuid
from datetime import datetime, timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import select, text

from lumen.services.key_expiry import MAX_KEY_TTL

from .conftest import TEST_CONFIG, flask_db

pytestmark = pytest.mark.postgres

_BEFORE = "b1c2d3e4f5a6"
_REVISION = "c2d3e4f5a6b7"
_CREATED = datetime(2026, 1, 1, 12, 0, 0)
_TTL_COMMENT = (
    "Key TTL in seconds (0 = never): as requested (null if none was), "
    "replaced by the approver's choice at approval"
)


def _seed(engine):
    with engine.begin() as conn:
        for code, status in [("AAAA-AAAA", "pending"), ("BBBB-BBBB", "approved")]:
            conn.execute(text(
                "INSERT INTO auth_requests (flow, client_id, requested_name, status, created_at,"
                " expires_at, user_code, overwrite)"
                " VALUES ('device', 'lumen-cli', 'opencode', :status, :t, :t, :code, false)"
            ), {"status": status, "t": _CREATED, "code": code})


def _ttl_column(conn):
    return conn.execute(text("""
        SELECT format_type(atttypid, atttypmod), col_description('auth_requests'::regclass, attnum)
        FROM pg_attribute WHERE attrelid = 'auth_requests'::regclass AND attname = 'ttl'
    """)).one_or_none()


def test_upgrade_leaves_existing_requests_null_and_downgrade_drops_column(pg_blank):
    url, engine = pg_blank
    flask_db(url, "upgrade", _BEFORE)
    _seed(engine)
    engine.dispose()

    flask_db(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        ttls = dict(conn.execute(text("SELECT user_code, ttl FROM auth_requests")).all())
        assert tuple(_ttl_column(conn)) == ("bigint", _TTL_COMMENT)
        # Every TTL the resolver allows fits, past the 32-bit boundary up to 100 years.
        for ttl in (2**31, MAX_KEY_TTL):
            conn.execute(text("UPDATE auth_requests SET ttl = :t WHERE user_code = 'AAAA-AAAA'"), {"t": ttl})
            assert conn.execute(text("SELECT ttl FROM auth_requests WHERE user_code = 'AAAA-AAAA'")).scalar() == ttl
        conn.rollback()
    assert ttls == {"AAAA-AAAA": None, "BBBB-BBBB": None}
    engine.dispose()

    flask_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        columns = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'auth_requests'"
        )).scalars())
        assert conn.execute(text("SELECT COUNT(*) FROM auth_requests")).scalar() == 2
    assert "ttl" not in columns


# ---------------------------------------------------------------------------
# Both OAuth flows on PostgreSQL with TTLs past the 32-bit boundary
# ---------------------------------------------------------------------------

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


@pytest.fixture
def approver(pg_app):
    """A user and a test client signed in as them; restores KEY_TTL afterwards."""
    from lumen.extensions import db, limiter
    from lumen.models.entity import Entity

    saved_cap = pg_app.config.get("KEY_TTL", 0)
    limiter.reset()
    with pg_app.app_context():
        user = Entity(entity_type="user", email=f"{uuid.uuid4().hex[:10]}@example.invalid",
                      name="User", initials="US", active=True)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = user_id
        sess["entity_name"] = "User"
        sess["initials"] = "US"
        sess["gravatar_hash"] = ""
    yield client
    pg_app.config["KEY_TTL"] = saved_cap


def _minted_expires_at(pg_app, name):
    from lumen.extensions import db
    from lumen.models.api_key import APIKey

    with pg_app.app_context():
        return db.session.execute(select(APIKey.expires_at).where(APIKey.name == name)).scalar_one()


def _assert_expires_in(pg_app, name, ttl):
    from lumen.timeutils import utcnow

    expected = utcnow() + timedelta(seconds=ttl)
    assert abs(_minted_expires_at(pg_app, name) - expected) < timedelta(minutes=1)


@pytest.mark.parametrize("cap, requested", [
    (0, 2**31),                   # uncapped: a requested TTL just past 32 bits
    (MAX_KEY_TTL, None),          # the largest cap (36500d), stored as the default
    (MAX_KEY_TTL, MAX_KEY_TTL),   # and requested explicitly
])
def test_device_flow_stores_and_mints_large_ttl(pg_app, approver, cap, requested):
    from lumen.extensions import db
    from lumen.models.auth_request import AuthRequest

    pg_app.config["KEY_TTL"] = cap
    name = f"big-{uuid.uuid4().hex[:8]}"
    form = {"client_id": "lumen-cli", "name": name}
    if requested is not None:
        form["ttl"] = str(requested)
    body = pg_app.test_client().post("/oauth/device_authorization", data=form)
    assert body.status_code == HTTPStatus.OK, body.get_json()
    body = body.get_json()

    assert approver.get(f"/device?code={body['user_code']}").status_code == HTTPStatus.OK
    with pg_app.app_context():
        req_id = db.session.execute(
            select(AuthRequest.id).where(AuthRequest.user_code == body["user_code"])
        ).scalar_one()
    approve = approver.post("/oauth/consent", data={"request_id": req_id, "action": "approve"})
    assert b"Key approved" in approve.data
    ttl = requested if requested is not None else cap
    with pg_app.app_context():
        assert db.session.get(AuthRequest, req_id).ttl == ttl

    token = pg_app.test_client().post("/oauth/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": body["device_code"], "client_id": "lumen-cli",
    })
    assert token.status_code == HTTPStatus.OK, token.get_json()
    assert token.get_json()["expires_at"] is not None
    _assert_expires_in(pg_app, name, ttl)


def test_code_flow_stores_and_mints_ttl_past_32_bits(pg_app, approver):
    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlencode, urlsplit

    from lumen.extensions import db
    from lumen.models.auth_request import AuthRequest

    pg_app.config["KEY_TTL"] = 0
    name = f"big-{uuid.uuid4().hex[:8]}"
    redirect = "https://portal.example.org/cb"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    page = approver.get("/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": "web-portal", "name": name, "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1", "ttl": str(2**31),
    }))
    assert page.status_code == HTTPStatus.OK
    with pg_app.app_context():
        req = db.session.execute(select(AuthRequest).where(AuthRequest.requested_name == name)).scalar_one()
        assert req.ttl == 2**31
        req_id = req.id
    approve = approver.post("/oauth/consent", data={"request_id": req_id, "action": "approve", "ttl": str(2**31)})
    assert approve.status_code == HTTPStatus.FOUND
    code = parse_qs(urlsplit(approve.headers["Location"]).query)["code"][0]

    token = pg_app.test_client().post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": "web-portal",
        "redirect_uri": redirect, "code_verifier": verifier,
    })
    assert token.status_code == HTTPStatus.OK, token.get_json()
    _assert_expires_in(pg_app, name, 2**31)
