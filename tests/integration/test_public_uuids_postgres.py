"""The public_uuids revision on real PostgreSQL + TimescaleDB.

The test steps this module's database back to the previous revision with the
same ``flask db`` command the container runs, seeds rows in all five tables
(users, a project, groups, API keys, conversations and auth requests), upgrades
again, and checks every row got its own non-null uuid, that the column is NOT
NULL with a unique index and its comment, and that the integer ids and foreign
keys are untouched. It then downgrades, checks the columns are gone, and
upgrades back.

A second test checks that the models and the migrated schema agree on the new
columns and indexes, so ``db.create_all`` and an upgraded database build the
same thing. It runs first, on the untouched upgrade.
"""

import os
import subprocess

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from lumen.extensions import db

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

PREVIOUS = "f4a5b6c7d8e9"
TABLES = ("entities", "groups", "api_keys", "conversations", "auth_requests")
COMMENT = (
    "Opaque public identifier for external use only (URLs, API payloads, CLI, session); "
    "never used as a key or join column"
)


def _flask_db(url, *args):
    env = {**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"}
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"flask db {' '.join(args)} failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def _has_uuid(conn, table):
    return conn.execute(text(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = :t AND column_name = 'uuid'"
    ), {"t": table}).scalar() == 1


def test_autogenerate_proposes_no_uuid_changes(pg_migrated_isolated):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), db.metadata)
    # Diffs come as tuples, or lists of tuples for column modifications.
    flat = [d for diff in diffs for d in (diff if isinstance(diff, list) else [diff])]
    assert [d for d in flat if "uuid" in repr(d)] == []


def test_upgrade_backfills_unique_uuids_and_downgrade_drops_them(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert not any(_has_uuid(conn, t) for t in TABLES)

    with engine.begin() as conn:
        users = [conn.execute(text(
            "INSERT INTO entities (entity_type, email, name, initials, active, store_conversations) "
            "VALUES ('user', :e, :e, 'U', true, true) RETURNING id"
        ), {"e": e}).scalar() for e in ("uuid-a@example.com", "uuid-b@example.com")]
        project = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active, store_conversations, owner_entity_id) "
            "VALUES ('project', 'uuid-project', 'UP', true, true, :o) RETURNING id"
        ), {"o": users[0]}).scalar()
        conn.execute(text(
            "INSERT INTO entity_managers (user_entity_id, project_entity_id) VALUES (:u, :p)"
        ), {"u": users[0], "p": project})
        for name in ("uuid-g1", "uuid-g2"):
            conn.execute(text(
                "INSERT INTO groups (name, active, auto_join) VALUES (:n, true, false)"
            ), {"n": name})
        keys = [conn.execute(text(
            "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
            " output_tokens, audio_seconds, cost, created_at)"
            " VALUES (:e, :n, :n, 0, 0, 0, 0, 0, now()) RETURNING id"
        ), {"e": project, "n": n}).scalar() for n in ("uuid-k1", "uuid-k2")]
        for _ in range(2):
            conn.execute(text("INSERT INTO conversations (entity_id) VALUES (:e)"), {"e": users[1]})
        for name in ("uuid-r1", "uuid-r2"):
            conn.execute(text(
                "INSERT INTO auth_requests (flow, client_id, requested_name, status, created_at,"
                " expires_at, overwrite, entity_id, api_key_id)"
                " VALUES ('code', 'cli', :n, 'claimed', now(), now(), false, :e, :k)"
            ), {"n": name, "e": users[0], "k": keys[0]})
        before = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES}
        assert all(before[t] >= 2 for t in TABLES), before

    _flask_db(url, "upgrade")

    with engine.connect() as conn:
        seen = []
        for table in TABLES:
            values = conn.execute(text(f"SELECT uuid FROM {table}")).scalars().all()
            assert len(values) == before[table]
            assert None not in values
            seen.extend(values)
            nullable, data_type = conn.execute(text(
                "SELECT is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'uuid'"
            ), {"t": table}).one()
            assert (nullable, data_type) == ("NO", "uuid")
            indexdef = conn.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i"
            ), {"t": table, "i": f"ix_{table}_uuid"}).scalar()
            assert indexdef and indexdef.startswith("CREATE UNIQUE INDEX"), indexdef
            comment = conn.execute(text(
                "SELECT col_description(CAST(:t AS regclass), attnum) FROM pg_attribute "
                "WHERE attrelid = CAST(:t AS regclass) AND attname = 'uuid'"
            ), {"t": table}).scalar()
            assert comment == COMMENT
        assert len(set(seen)) == len(seen)
        # Integer ids and foreign keys are untouched.
        assert conn.execute(text(
            "SELECT owner_entity_id FROM entities WHERE id = :p"
        ), {"p": project}).scalar() == users[0]
        assert conn.execute(text(
            "SELECT count(*) FROM auth_requests WHERE api_key_id = :k"
        ), {"k": keys[0]}).scalar() == 2

    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert not any(_has_uuid(conn, t) for t in TABLES)
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == before
    _flask_db(url, "upgrade")
