"""The c93d4de8245c messages index revision on real PostgreSQL.

The revision builds its indexes ``CONCURRENTLY`` inside an ``autocommit_block``
on PostgreSQL only, and drops an INVALID leftover of a failed concurrent build
before building again, so all of it is only provable here. The tests step this
module's private database back below the revision and up again, so they run
against ``pg_migrated_isolated`` and leave it at head.
"""
import os
import subprocess

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from lumen.models import Message
from tests.integration.conftest import TEST_CONFIG
from tests.unit.test_migrations import _script_dir

pytestmark = pytest.mark.postgres

_BEFORE = "a827b0223638"
_REVISION = "c93d4de8245c"
_COMPOSITE = "ix_messages_conversation_id_created_at"
_SINGLE = "ix_messages_conversation_id"


def _flask_db(url, *args):
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"},
    )
    assert result.returncode == 0, (
        f"flask db {' '.join(args)} failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _messages_indexes(conn):
    """``{name: (indexdef, indisvalid)}`` for every index on ``messages``."""
    rows = conn.execute(text(
        "SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisvalid "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indrelid = CAST('messages' AS regclass)"
    )).all()
    return {name: (indexdef, valid) for name, indexdef, valid in rows}


def _assert_at_head(engine):
    with engine.connect() as conn:
        indexes = _messages_indexes(conn)
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        diffs = compare_metadata(MigrationContext.configure(conn), Message.metadata)
    assert version == _script_dir().get_current_head()
    assert _SINGLE not in indexes
    indexdef, valid = indexes[_COMPOSITE]
    assert indexdef.endswith("(conversation_id, created_at)")
    assert valid
    messages_diffs = [
        diff for diff in diffs
        if isinstance(diff, tuple) and getattr(diff[1], "table", None) is not None
        and diff[1].table.name == "messages"
    ]
    assert messages_diffs == []


def test_round_trip(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    engine.dispose()

    _flask_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        indexes = _messages_indexes(conn)
    assert _COMPOSITE not in indexes
    assert indexes[_SINGLE][1]
    engine.dispose()

    _flask_db(url, "upgrade")
    _assert_at_head(engine)
    engine.dispose()


def _index_oid(conn, name):
    return conn.execute(text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}).scalar()


def test_upgrade_rebuilds_an_invalid_leftover(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    engine.dispose()

    _flask_db(url, "downgrade", _BEFORE)
    # What a failed or interrupted CREATE INDEX CONCURRENTLY leaves behind.
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX {_COMPOSITE} ON messages (conversation_id, created_at)"))
        conn.execute(text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:name AS regclass)"
        ), {"name": _COMPOSITE})
    with engine.connect() as conn:
        assert _messages_indexes(conn)[_COMPOSITE][1] is False
        leftover = _index_oid(conn, _COMPOSITE)
    engine.dispose()

    _flask_db(url, "upgrade")
    _assert_at_head(engine)
    with engine.connect() as conn:
        assert _index_oid(conn, _COMPOSITE) != leftover
    engine.dispose()


def test_upgrade_keeps_a_valid_leftover(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    engine.dispose()

    _flask_db(url, "downgrade", _BEFORE)
    # What a run that died after the concurrent build but before the version stamp leaves behind.
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX {_COMPOSITE} ON messages (conversation_id, created_at)"))
    with engine.connect() as conn:
        assert _messages_indexes(conn)[_COMPOSITE][1] is True
        leftover = _index_oid(conn, _COMPOSITE)
    engine.dispose()

    _flask_db(url, "upgrade")
    _assert_at_head(engine)
    with engine.connect() as conn:
        assert _index_oid(conn, _COMPOSITE) == leftover
    engine.dispose()


def _assert_commit_before(sql, statement):
    assert statement in sql, sql
    position = sql.index(statement)
    assert "COMMIT;" in sql[:position], sql
    assert "BEGIN;" in sql[position:], sql


def test_offline_upgrade_emits_concurrent_ddl(pg_migrated_isolated):
    url, _ = pg_migrated_isolated
    sql = _flask_db(url, "upgrade", f"{_BEFORE}:{_REVISION}", "--sql")

    _assert_commit_before(
        sql, f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_COMPOSITE} ON messages (conversation_id, created_at)"
    )
    _assert_commit_before(sql, f"DROP INDEX CONCURRENTLY IF EXISTS {_SINGLE}")


def test_offline_downgrade_emits_concurrent_ddl(pg_migrated_isolated):
    url, _ = pg_migrated_isolated
    sql = _flask_db(url, "downgrade", f"{_REVISION}:{_BEFORE}", "--sql")

    _assert_commit_before(sql, f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_SINGLE} ON messages (conversation_id)")
    _assert_commit_before(sql, f"DROP INDEX CONCURRENTLY IF EXISTS {_COMPOSITE}")
