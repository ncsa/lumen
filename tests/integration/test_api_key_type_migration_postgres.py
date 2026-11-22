"""The api_keys.key_type revision on real PostgreSQL.

The test steps this module's database back to the previous head, seeds a
user key, a project key and an OAuth key, upgrades again and checks that every
one of them reads ``normal``, that the column is NOT NULL with its comment and
default, and that ``ck_api_keys_key_type`` refuses other values. It then
downgrades and checks the column and the constraint are gone.

A second test checks that the model and the migrated schema agree on the
column and its constraint, so ``db.create_all`` and an upgraded database build
the same thing. It runs first, on the untouched upgrade.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lumen.models.api_key import APIKey

from .conftest import migrate_db

pytestmark = pytest.mark.postgres

PREVIOUS = "2ea58823bdc7"
CHECK = "ck_api_keys_key_type"


def _column(conn):
    """(is_nullable, column_default, comment) of api_keys.key_type, or None."""
    row = conn.execute(text(
        "SELECT c.is_nullable, c.column_default, col_description('api_keys'::regclass, c.ordinal_position) "
        "FROM information_schema.columns c "
        "WHERE c.table_name = 'api_keys' AND c.column_name = 'key_type'"
    )).one_or_none()
    return tuple(row) if row else None


def _check(conn):
    return conn.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = :n AND conrelid = 'api_keys'::regclass"
    ), {"n": CHECK}).scalar()


def _insert_key(conn, entity_id, name, key_type=None, client_id=None):
    columns, values = "", ""
    if key_type is not None:
        columns, values = ", key_type", ", :t"
    return conn.execute(text(
        "INSERT INTO api_keys (entity_id, name, key_hash, client_id, requests, input_tokens,"
        f" output_tokens, audio_seconds, cost, created_at{columns})"
        f" VALUES (:e, :n, :n, :c, 0, 0, 0, 0, 0, now(){values}) RETURNING id"
    ), {"e": entity_id, "n": name, "c": client_id, "t": key_type}).scalar()


def test_autogenerate_proposes_no_key_type_changes(pg_migrated_isolated):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), APIKey.metadata)
    # Diffs come as tuples, or lists of tuples for column modifications.
    flat = [d for diff in diffs for d in (diff if isinstance(diff, list) else [diff])]
    assert [d for d in flat if "key_type" in repr(d)] == []


def test_upgrade_makes_every_existing_key_normal(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    migrate_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert _column(conn) is None

    with engine.begin() as conn:
        user_id = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'key-type-user', 'KT', true) RETURNING id"
        )).scalar()
        project_id = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active, owner_entity_id) "
            "VALUES ('project', 'key-type-project', 'KP', true, :o) RETURNING id"
        ), {"o": user_id}).scalar()
        conn.execute(text(
            "INSERT INTO entity_managers (user_entity_id, project_entity_id, role) VALUES (:u, :p, 'manager')"
        ), {"u": user_id, "p": project_id})
        keys = {
            "user": _insert_key(conn, user_id, "kt-user"),
            "project": _insert_key(conn, project_id, "kt-project"),
            "oauth": _insert_key(conn, user_id, "kt-oauth", client_id="cli"),
        }

    migrate_db(url, "upgrade", "head")

    with engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT id, key_type FROM api_keys WHERE id = ANY(:ids)"
        ), {"ids": list(keys.values())}).all())
        assert types == dict.fromkeys(keys.values(), "normal")
        nullable, default, comment = _column(conn)
        assert nullable == "NO"
        assert default.startswith("'normal'")
        assert comment.startswith("Key type: 'normal'")
        assert "'admin'" in _check(conn) and "'monitoring'" in _check(conn)

    with engine.begin() as conn:
        assert _insert_key(conn, user_id, "kt-admin", key_type="admin")
    with pytest.raises(IntegrityError, match=CHECK):
        with engine.begin() as conn:
            _insert_key(conn, user_id, "kt-bad", key_type="root")

    migrate_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        assert _column(conn) is None
        assert _check(conn) is None
    migrate_db(url, "upgrade", "head")
