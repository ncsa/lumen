"""``flask erase-entity`` against compressed request_logs, real aggregates and real foreign keys.

The SQLite unit tests (``tests/unit/test_erase_entity_cmd.py``) cover what the command
deletes, moves and anonymises, but SQLite has no hypertable, no continuous aggregate
and enforces no foreign key here. This module checks the PostgreSQL-only half:

- the user's history sits in a genuinely compressed chunk, and ``entity_id`` is not a
  ``segmentby`` column, so every chunk UPDATE decompresses; as in
  ``test_reassign_model_postgres.py`` the guard is lowered to 10 at database level, so
  a move that is not split per chunk with the limit lifted aborts here on 60 rows;
- ``request_counts_hourly_by_entity`` ends with the history under the ghost, and the
  all-users ``/usage`` summary (``request_counts_hourly``) does not change;
- the cascades and SET NULLs stay as a backstop, so the same "no dependent row
  remains" and key checks as the unit tests must hold here too;
- a run interrupted in the chunk walk or in the refresh resumes, and a row the walk
  missed makes the final delete fail on the RESTRICT foreign key, rolling the whole
  final transaction back.

``request_logs.entity_id`` is still ``ON DELETE SET NULL`` until #176 lands its
RESTRICT foreign key. ``pg_app`` swaps it for RESTRICT in this module's private
database, the constraint #176 adds, so the rollback test checks the command's
behaviour against the schema it relies on; once #176 is in, the swap is a no-op.

The ghost is a single shared row, so assertions on it are deltas or on the rows each
test seeded. Every test uses its own users and seeds the same two ages (40 days,
compressed, and 2 days), so tests share only chunks, and every test leaves those fully
compressed.
"""

import os
import re
import uuid
from datetime import datetime
from http import HTTPStatus
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, exc, func, select, text

from lumen import commands
from lumen.commands import erase_entity_cmd
from tests.conftest import no_snapshot_refresher

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

OLD_ROWS = 60  # well above the lowered guard (10)
RECENT_ROWS = 3
SEEDED = OLD_ROWS + RECENT_ROWS
ENTITY_AGGREGATE = "request_counts_hourly_by_entity"
EXPIRES = datetime(2030, 1, 1)
CREATED = datetime(2025, 1, 2, 3, 4, 5)
REVOKED = datetime(2025, 6, 1, 12, 0, 0)


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A Flask app on this module's database: decompression guard at 10, RESTRICT on entity_id.

    Both are set before the app opens a connection, so every pooled connection starts
    with them.
    """
    url = pg_migrated_isolated[0]
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'ALTER DATABASE "{url.rsplit("/", 1)[1]}" SET timescaledb.max_tuples_decompressed_per_dml_transaction = 10'))
        _restrict_entity_fk(conn)
    admin.dispose()

    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    from config import Config

    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app

    with pytest.MonkeyPatch.context() as mp:
        no_snapshot_refresher(mp)
        application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        from lumen.extensions import db

        assert db.engine.dialect.name == "postgresql"
        guard = db.session.execute(text("SHOW timescaledb.max_tuples_decompressed_per_dml_transaction")).scalar()
        assert guard == "10", f"the decompression guard is {guard}, not 10; an unsplit move would silently decompress instead of aborting"
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


def _entity_fk(conn):
    """``(name, confdeltype)`` of the foreign key from request_logs.entity_id to entities."""
    return conn.execute(text(
        "SELECT conname, confdeltype FROM pg_constraint "
        "WHERE conrelid = 'request_logs'::regclass AND contype = 'f' "
        "AND confrelid = 'entities'::regclass"
    )).one()


def _restrict_entity_fk(conn):
    """Make request_logs.entity_id ON DELETE RESTRICT, as #176 does, unless it already is."""
    name, rule = _entity_fk(conn)
    if rule != "r":
        conn.execute(text(f'ALTER TABLE request_logs DROP CONSTRAINT "{name}"'))
        conn.execute(text(
            f'ALTER TABLE request_logs ADD CONSTRAINT "{name}" '
            "FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE RESTRICT"
        ))
    assert _entity_fk(conn)[1] == "r"


@pytest.fixture
def engine(pg_app):
    with pg_app.app_context():
        from lumen.extensions import db

        yield db.engine


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


# --------------------------------------------------------------------------- #
# Seeding                                                                      #
# --------------------------------------------------------------------------- #

def _seed_world(pg_app):
    """A user with every kind of personal data the command handles, and the entities around it.

    Mirrors the unit tests' ``world``: the user's keys (one active with OAuth
    provenance, one already revoked), a conversation with messages, an auth request,
    balance, limit, consent, a private model, stats; another user owning a project the
    user manages, a project key the user created and revoked, another user's auth
    request referencing the user's key, a group the user owns and one they are in.
    """
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.auth_request import AuthRequest
    from lumen.models.conversation import Conversation
    from lumen.models.entity import Entity
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.models.entity_manager import EntityManager
    from lumen.models.entity_model_consent import EntityModelConsent
    from lumen.models.entity_stat import EntityStat
    from lumen.models.group import Group
    from lumen.models.group_member import GroupMember
    from lumen.models.message import Message
    from lumen.models.model_config import ModelConfig
    from lumen.models.model_stat import ModelStat
    from lumen.services.crypto import hash_api_key
    from tests.conftest import make_project

    tag = uuid.uuid4().hex[:8]
    with pg_app.app_context():
        def key(entity_id, raw, **fields):
            k = APIKey(entity_id=entity_id, name=fields.pop("name", f"key {raw}"), key_hash=hash_api_key(raw),
                       key_hint=raw[-4:], created_at=CREATED, **fields)
            db.session.add(k)
            db.session.flush()
            return k

        model = ModelConfig(model_name=f"erase-{tag}", input_cost_per_million=1, output_cost_per_million=1)
        user = Entity(entity_type="user", email=f"erin-{tag}@example.com", name=f"Erin {tag}",
                      initials="ER", active=True)
        other = Entity(entity_type="user", email=f"olga-{tag}@example.com", name=f"Olga {tag}",
                       initials="OL", active=True)
        db.session.add_all([model, user, other])
        db.session.flush()
        private = ModelConfig(model_name=f"erin-private-{tag}", input_cost_per_million=1,
                              output_cost_per_million=1, owner_entity_id=user.id)
        active = key(user.id, f"sk-{tag}-active", client_id="cli", requested_by="laptop",
                     created_by_entity_id=user.id, expires_at=EXPIRES, requests=3, input_tokens=30)
        revoked = key(user.id, f"sk-{tag}-revoked", revoked_at=REVOKED, revoked_by_entity_id=user.id)
        conv = Conversation(entity_id=user.id, title="secret", model=model.model_name)
        other_conv = Conversation(entity_id=other.id, title="olga's", model=model.model_name)
        db.session.add_all([private, conv, other_conv])
        db.session.flush()
        db.session.add_all([
            Message(conversation_id=conv.id, role="user", content="my secret"),
            Message(conversation_id=conv.id, role="assistant", content="noted"),
            Message(conversation_id=other_conv.id, role="user", content="hi"),
            AuthRequest(flow="device", client_id="cli", requested_name="k", status="approved",
                        expires_at=EXPIRES, entity_id=user.id),
            AuthRequest(flow="code", client_id="app", requested_name="k", status="claimed",
                        expires_at=EXPIRES, entity_id=other.id, api_key_id=active.id),
            EntityBalance(entity_id=user.id, coins_left=5),
            EntityLimit(entity_id=user.id, max_coins=10, refresh_coins=1, starting_coins=10),
            EntityModelConsent(entity_id=user.id, model_config_id=model.id),
            ModelStat(entity_id=user.id, model_config_id=model.id, source="api", requests=SEEDED,
                      input_tokens=SEEDED, output_tokens=SEEDED, cost=0.63),
            EntityStat(entity_id=user.id, requests=SEEDED, input_tokens=SEEDED, output_tokens=SEEDED,
                       audio_seconds=0, cost=0.63, conversations=1),
        ])
        db.session.commit()
        project = make_project(f"Lab {tag}", owner_id=other.id)
        db.session.add(EntityManager(user_entity_id=user.id, project_entity_id=project.id))
        project_key = key(project.id, f"sk-{tag}-project", created_by_entity_id=user.id,
                          revoked_at=REVOKED, revoked_by_entity_id=user.id)
        owned = Group(name=f"erin-owned-{tag}", active=True)
        member = Group(name=f"erin-member-{tag}", active=True)
        db.session.add_all([owned, member])
        db.session.flush()
        db.session.add_all([
            GroupMember(group_id=owned.id, entity_id=user.id, is_owner=True),
            GroupMember(group_id=owned.id, entity_id=other.id),
            GroupMember(group_id=member.id, entity_id=user.id),
            GroupMember(group_id=member.id, entity_id=other.id, is_owner=True),
        ])
        db.session.commit()
        return {
            "tag": tag, "user": user.id, "other": other.id, "model": model.id, "private": private.id,
            "active_key": active.id, "revoked_key": revoked.id, "project": project.id,
            "project_key": project_key.id, "conv": conv.id, "other_conv": other_conv.id,
            "owned_group": owned.id, "member_group": member.id,
            "raw_keys": (f"sk-{tag}-active", f"sk-{tag}-revoked"),
        }


def _seed(engine, entity_id, model_id, age, rows):
    with engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO request_logs (time, entity_id, model_config_id, source, input_tokens, output_tokens, cost, duration) "
            f"SELECT now() - INTERVAL '{age}', :e, :m, 'api', 1, 1, 0.01, 0.1 FROM generate_series(1, :r) "
            "RETURNING id"
        ), {"e": entity_id, "m": model_id, "r": rows}).scalars().all()


def _seed_history(engine, world):
    """60 user rows 40 days old in a compressed chunk, a few recent ones, aggregates backfilled."""
    ids = _seed(engine, world["user"], world["model"], "40 days", OLD_ROWS)
    ids += _seed(engine, world["user"], world["model"], "2 days", RECENT_ROWS)
    with _autocommit(engine) as conn:
        # recompress => true: an earlier test may have compressed this chunk already,
        # and rows inserted since sit in its heap.
        conn.execute(text(
            "SELECT compress_chunk(c, recompress => true) "
            "FROM show_chunks('request_logs', older_than => INTERVAL '7 days') c"
        ))
        conn.execute(text("CALL refresh_continuous_aggregate('request_counts_hourly', now() - INTERVAL '60 days', now())"))
        conn.execute(text(f"CALL refresh_continuous_aggregate('{ENTITY_AGGREGATE}', now() - INTERVAL '60 days', now())"))
    assert _compressed_chunks(engine), "nothing was compressed; this test would prove nothing about compressed history"
    assert _materialised(engine, world["user"]) == SEEDED
    world["logs"] = ids
    return ids


# --------------------------------------------------------------------------- #
# Reading back                                                                 #
# --------------------------------------------------------------------------- #

def _compressed_chunks(engine):
    """``{chunk: rows in its uncompressed heap}`` for every chunk flagged compressed."""
    with engine.connect() as conn:
        chunks = conn.execute(text(
            "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND is_compressed"
        )).scalars().all()
        return {c: conn.execute(text(f"SELECT count(*) FROM ONLY {c}")).scalar() for c in chunks}


def _materialised(engine, entity_id):
    """Requests materialised under ``entity_id`` in the per-entity aggregate (no retention)."""
    with engine.connect() as conn:
        table = conn.execute(text(
            "SELECT format('%I.%I', materialization_hypertable_schema, materialization_hypertable_name) "
            "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
        ), {"v": ENTITY_AGGREGATE}).scalar()
        return conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {table} WHERE entity_id = :e"
        ), {"e": entity_id}).scalar()


def _raw(engine, entity_id):
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM request_logs WHERE entity_id = :e"),
                            {"e": entity_id}).scalar()


def _owners(engine, log_ids):
    with engine.connect() as conn:
        return conn.execute(text("SELECT entity_id, count(*) FROM request_logs WHERE id = ANY(:ids) "
                                 "GROUP BY entity_id"), {"ids": log_ids}).all()


def _ghost_id(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT id FROM entities WHERE entity_type = 'ghost'")).scalar()


def _ghost_raw(engine):
    ghost = _ghost_id(engine)
    return 0 if ghost is None else _raw(engine, ghost)


def _stat_totals(engine):
    with engine.connect() as conn:
        return (
            conn.execute(text("SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(input_tokens), 0), "
                              "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost), 0) FROM model_stats")).one(),
            conn.execute(text("SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(input_tokens), 0), "
                              "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost), 0), "
                              "COALESCE(SUM(conversations), 0) FROM entity_stats")).one(),
        )


def _all_users_summary(pg_app):
    """The all-users ``/usage`` summary, as an admin with no entity selected."""
    from lumen.extensions import db
    from lumen.models.entity import Entity

    with pg_app.app_context():
        admin = db.session.execute(select(Entity).filter_by(email="admin@example.com")).scalar_one_or_none()
        if admin is None:
            admin = Entity(entity_type="user", email="admin@example.com", name="Admin", initials="AD", active=True)
            db.session.add(admin)
            db.session.commit()
        admin_id = admin.id
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = admin_id
        sess["admin_mode"] = True
    resp = client.get("/api/usage/summary?period=all")
    assert resp.status_code == HTTPStatus.OK, resp.data
    body = resp.get_json()
    return body["requests"], body["tokens"], body["cost"]


def _run(pg_app, entity_id):
    return pg_app.test_cli_runner().invoke(erase_entity_cmd, [str(entity_id), "--yes"])


def _assert_history_moved(engine, world, ghost_raw_before):
    ghost = _ghost_id(engine)
    assert _owners(engine, world["logs"]) == [(ghost, SEEDED)]
    assert _raw(engine, world["user"]) == 0
    assert _raw(engine, ghost) == ghost_raw_before + SEEDED
    assert _materialised(engine, world["user"]) == 0, f"{ENTITY_AGGREGATE} still has buckets under the user"
    assert _materialised(engine, ghost) == _raw(engine, ghost), f"{ENTITY_AGGREGATE} totals under the ghost do not match raw"
    assert set(_compressed_chunks(engine).values()) <= {0}, "a compressed chunk was left with rows in its heap"


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_moves_compressed_history_to_the_ghost_and_keeps_totals(pg_app, engine):
    world = _seed_world(pg_app)
    _seed_history(engine, world)
    ghost_raw = _ghost_raw(engine)
    summary = _all_users_summary(pg_app)
    totals = _stat_totals(engine)

    result = _run(pg_app, world["user"])

    assert result.exit_code == 0, result.output
    assert f"request_logs moved to the ghost: {SEEDED}" in result.output
    assert ": recompressed" in result.output
    assert f"refreshing '{ENTITY_AGGREGATE}' from" in result.output
    _assert_history_moved(engine, world, ghost_raw)
    assert _all_users_summary(pg_app) == summary
    assert _stat_totals(engine) == totals

    # Guard leak: both lifts were SET LOCAL, so a pooled connection still has 10.
    with engine.connect() as conn:
        assert conn.execute(text("SHOW timescaledb.max_tuples_decompressed_per_dml_transaction")).scalar() == "10"

    # Control: the unsplit UPDATE aborts against the guard. If it stops raising, the
    # guard is not enforced and the move above proved nothing.
    ghost = _ghost_id(engine)
    with pytest.raises(exc.DBAPIError) as aborted:
        with engine.begin() as conn:
            conn.execute(text("UPDATE request_logs SET entity_id = :o WHERE entity_id = :g"),
                         {"o": world["other"], "g": ghost})
    assert "decompression limit" in str(aborted.value), f"the control UPDATE failed with something else: {aborted.value}"


def test_no_dependent_row_remains_and_keys_are_anonymised(pg_app, engine):
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.auth_request import AuthRequest
    from lumen.models.group_member import get_group_owner
    from lumen.models.model_config import ModelConfig
    from lumen.services.crypto import hash_api_key

    world = _seed_world(pg_app)
    _seed_history(engine, world)
    with engine.connect() as conn:
        old = {r.id: r for r in conn.execute(text(
            "SELECT id, key_hash, requests, input_tokens, output_tokens, audio_seconds, cost, "
            "created_at, last_used_at, expires_at, revoked_at FROM api_keys WHERE entity_id = :u"
        ), {"u": world["user"]})}
    assert set(old) == {world["active_key"], world["revoked_key"]}

    result = _run(pg_app, world["user"])

    assert result.exit_code == 0, result.output
    for line in ("messages deleted: 2", "conversations deleted: 1", "auth_requests deleted: 1",
                 "entity_balances deleted: 1", "entity_limits deleted: 1",
                 "entity_model_consents deleted: 1", "group_members deleted: 2",
                 "entity_managers deleted: 1", "api_keys revoked, anonymised and moved to the ghost: 2",
                 f"group left without an owner: erin-owned-{world['tag']}", f"user {world['user']} deleted"):
        assert line in result.output

    u = world["user"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM entities WHERE id = :u"), {"u": u}).scalar() == 0
        # Every foreign key into entities, read from the catalog rather than listed, so
        # a table added later is checked too.
        fks = conn.execute(text(
            "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'f' AND c.confrelid = 'entities'::regclass "
            "AND c.conrelid::regclass::text NOT LIKE '\\_timescaledb%'"
        )).all()
        assert len(fks) >= 15, fks
        for table, column in fks:
            assert conn.execute(text(f'SELECT count(*) FROM {table} WHERE "{column}" = :u'), {"u": u}).scalar() == 0, \
                f"{table}.{column} still references the erased user"
        assert conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"),
                            {"c": world["conv"]}).scalar() == 0

    with pg_app.app_context():
        ghost = _ghost_id(engine)
        keys = db.session.execute(select(APIKey).where(APIKey.id.in_(old))).scalars().all()
        hashes = set()
        for k in keys:
            was = old[k.id]
            assert k.entity_id == ghost
            assert k.name == "ghost key"
            assert (k.key_hint, k.client_id, k.requested_by, k.created_by_entity_id, k.revoked_by_entity_id) == \
                (None, None, None, None, None)
            assert re.fullmatch(r"[0-9a-f]{64}", k.key_hash) and k.key_hash != was.key_hash
            hashes.add(k.key_hash)
            assert (k.requests, k.input_tokens, k.output_tokens, k.audio_seconds, k.cost,
                    k.created_at, k.last_used_at, k.expires_at) == tuple(was)[2:10]
            assert k.revoked_at is not None
        assert len(hashes) == 2
        assert db.session.get(APIKey, world["revoked_key"]).revoked_at == REVOKED
        for raw in world["raw_keys"]:
            assert db.session.execute(select(APIKey).where(APIKey.key_hash == hash_api_key(raw))).first() is None

        private = db.session.get(ModelConfig, world["private"])
        assert private.disabled is True and private.owner_entity_id == ghost
        project_key = db.session.get(APIKey, world["project_key"])
        assert project_key.entity_id == world["project"]
        assert project_key.created_by_entity_id is None and project_key.revoked_by_entity_id is None
        other_request = db.session.execute(
            select(AuthRequest).where(AuthRequest.entity_id == world["other"])).scalar_one()
        assert other_request.api_key_id == world["active_key"]
        assert get_group_owner(world["owned_group"]) is None
        assert get_group_owner(world["member_group"]).id == world["other"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"),
                            {"c": world["other_conv"]}).scalar() == 1

    client = pg_app.test_client()
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {world['raw_keys'][0]}"}).status_code \
        == HTTPStatus.UNAUTHORIZED


def test_resumes_after_a_chunk_fails(pg_app, engine):
    """The second chunk's UPDATE fails: exit 1, every row owned by the user or the
    ghost, the user still there; a re-run finishes, refresh included."""
    world = _seed_world(pg_app)
    _seed_history(engine, world)
    ghost_raw = _ghost_raw(engine)
    real = commands._move_chunk
    calls = []

    def fail_second(*args):
        calls.append(args)
        if len(calls) == 2:
            raise exc.DBAPIError("UPDATE request_logs", {}, Exception("injected"))
        return real(*args)

    with patch.object(commands, "_move_chunk", side_effect=fail_second):
        result = _run(pg_app, world["user"])
    assert result.exit_code == 1, result.output
    assert "Chunks done: " in result.output

    ghost = _ghost_id(engine)
    owners = dict(_owners(engine, world["logs"]))
    assert set(owners) == {world["user"], ghost}, f"a row is owned by neither the user nor the ghost: {owners}"
    assert sum(owners.values()) == SEEDED
    with engine.connect() as conn:
        assert conn.execute(text("SELECT active FROM entities WHERE id = :u"), {"u": world["user"]}).scalar() is False
        assert conn.execute(text("SELECT count(*) FROM conversations WHERE entity_id = :u"),
                            {"u": world["user"]}).scalar() == 1

    result = _run(pg_app, world["user"])
    assert result.exit_code == 0, result.output
    _assert_history_moved(engine, world, ghost_raw)


def test_resumes_after_the_refresh_fails(pg_app, engine):
    """The user is deleted but no bucket refreshed: re-running with the deleted id finds
    the id only in the materialised buckets and refreshes from the oldest of them."""
    world = _seed_world(pg_app)
    _seed_history(engine, world)
    ghost_raw = _ghost_raw(engine)

    def fail(conn, name, start, now):
        raise commands._RefreshFailed(start, [], exc.DBAPIError("CALL", {}, Exception("injected")))

    with patch.object(commands, "_refresh_months", side_effect=fail):
        result = _run(pg_app, world["user"])
    assert result.exit_code == 1, result.output
    assert f"user {world['user']} deleted" in result.output
    assert _raw(engine, world["user"]) == 0
    assert _materialised(engine, world["user"]) == SEEDED

    result = _run(pg_app, world["user"])
    assert result.exit_code == 0, result.output
    assert "already deleted; finishing its aggregate refresh" in result.output
    _assert_history_moved(engine, world, ghost_raw)

    result = _run(pg_app, world["user"])
    assert result.exit_code == 0, result.output
    assert "nothing is left under it: nothing to do" in result.output


def test_a_row_the_walk_missed_fails_restrict_and_rolls_back(pg_app, engine):
    """A row logged under the user after the straggler sweep makes the final DELETE fail
    on the RESTRICT foreign key. Everything the final transaction did is rolled back;
    a re-run sweeps the row up and succeeds."""
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_config import ModelConfig
    from lumen.models.model_stat import ModelStat
    from lumen.services.crypto import hash_api_key

    world = _seed_world(pg_app)
    _seed_history(engine, world)
    ghost_raw = _ghost_raw(engine)
    late = []
    real = commands._ghost_keys

    def log_late_row(user_id, ghost_id):
        # Committed by another connection after the sweep, as a request in flight would.
        late.extend(_seed(engine, user_id, world["model"], "2 days", 1))
        return real(user_id, ghost_id)

    with patch.object(commands, "_ghost_keys", side_effect=log_late_row):
        result = _run(pg_app, world["user"])

    assert result.exit_code == 1, result.output
    assert "request_logs" in result.output and "re-run" in result.output
    assert f"user {world['user']} deleted" not in result.output
    u = world["user"]
    with pg_app.app_context():
        assert db.session.get(commands.Entity, u) is not None
        keys = {k.id: k for k in db.session.execute(select(APIKey).filter_by(entity_id=u)).scalars()}
        assert set(keys) == {world["active_key"], world["revoked_key"]}
        assert keys[world["active_key"]].name == f"key {world['raw_keys'][0]}"
        assert keys[world["active_key"]].key_hash == hash_api_key(world["raw_keys"][0])
        assert keys[world["active_key"]].revoked_at is None
        assert keys[world["revoked_key"]].key_hash == hash_api_key(world["raw_keys"][1])
        assert db.session.get(APIKey, world["project_key"]).created_by_entity_id == u
        assert db.session.scalar(select(func.count()).select_from(ModelStat).filter_by(entity_id=u)) == 1
        assert db.session.get(EntityStat, u).requests == SEEDED
        private = db.session.get(ModelConfig, world["private"])
        assert private.owner_entity_id == u and not private.disabled
    with engine.connect() as conn:
        for table, column in (("conversations", "entity_id"), ("auth_requests", "entity_id"),
                              ("entity_balances", "entity_id"), ("group_members", "entity_id"),
                              ("entity_managers", "user_entity_id")):
            assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE {column} = :u"), {"u": u}).scalar() > 0, table
        assert conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"),
                            {"c": world["conv"]}).scalar() == 2
    # Committed per chunk before the final transaction: the walk's rows stay with the ghost.
    assert _owners(engine, world["logs"]) == [(_ghost_id(engine), SEEDED)]
    assert _owners(engine, late) == [(u, 1)]

    result = _run(pg_app, u)
    assert result.exit_code == 0, result.output
    assert "request_logs moved to the ghost: 1" in result.output
    ghost = _ghost_id(engine)
    assert _owners(engine, world["logs"] + late) == [(ghost, SEEDED + 1)]
    assert _raw(engine, u) == 0 and _raw(engine, ghost) == ghost_raw + SEEDED + 1
    assert _materialised(engine, u) == 0
    assert _materialised(engine, ghost) == _raw(engine, ghost)


def test_refuses_with_a_retention_policy_on_request_logs(pg_app, engine):
    world = _seed_world(pg_app)
    _seed(engine, world["user"], world["model"], "2 days", 1)
    with _autocommit(engine) as conn:
        conn.execute(text("SELECT add_retention_policy('request_logs', INTERVAL '400 days')"))
    try:
        result = _run(pg_app, world["user"])
    finally:
        with _autocommit(engine) as conn:
            conn.execute(text("SELECT remove_retention_policy('request_logs')"))

    assert result.exit_code == 1, result.output
    assert "flask check-aggregate" in result.output
    assert _raw(engine, world["user"]) == 1, "rows moved before the refusal"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT active FROM entities WHERE id = :u"), {"u": world["user"]}).scalar() is True
