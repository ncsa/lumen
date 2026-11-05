"""The request_logs_fk_restrict revision on real PostgreSQL + TimescaleDB.

``request_logs.entity_id``, ``model_config_id``, ``model_endpoint_id`` and
``api_key_id`` are ``ON DELETE RESTRICT``: a direct ``DELETE`` of a parent that has request
history must fail rather than rewrite the compressed hypertable. Only a real
TimescaleDB can show that the action reaches every chunk, that the check sees
rows that exist only in a compressed chunk, and that the migration's
drop-and-re-add of each FK survives a compressed chunk in both directions.
``api_key_id`` came later (``c5d7e9f1a3b6``, ``NOT VALID``); it is held to the
same RESTRICT checks at head, and the round trip below this revision drops it.

The seeded history row sits in a closed chunk that the module fixture
compresses once. Its endpoint belongs to a second model, so deleting the
row's model is refused by the model FK itself and not by the endpoint FK
behind the ``model_endpoints`` cascade. The round trip steps this module's
private database back below the revision and up again, so it runs against
``pg_migrated_isolated`` and leaves it at head.
"""
import os
import subprocess

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lumen.models import RequestLog
from tests.integration.conftest import TEST_CONFIG
from tests.unit.test_migrations import _script_dir

pytestmark = pytest.mark.postgres

_BEFORE = "b4c6d8e0f2a4"
_HEAD = _script_dir().get_current_head()
_SOURCE = "fkrestr"  # request_logs.source is VARCHAR(8)

# FK column -> (constraint name, parent table)
_FKS = {
    "entity_id": ("request_logs_entity_id_fkey", "entities"),
    "model_config_id": ("request_logs_model_config_id_fkey", "model_configs"),
    "model_endpoint_id": ("request_logs_model_endpoint_id_fkey", "model_endpoints"),
    "api_key_id": ("fk_request_logs_api_key_id", "api_keys"),
}
# The FKs c8d0e2f4a6b8 switches; api_key_id is added by a later revision.
_REVISION_FKS = ("entity_id", "model_config_id", "model_endpoint_id")

# Every FK on request_logs or one of its chunks, by referenced column.
# confdeltype: 'r' = RESTRICT, 'n' = SET NULL.
_FK_ACTIONS = """
    SELECT c.conrelid::regclass::text AS rel, a.attname AS col, c.confdeltype AS action
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.contype = 'f'
      AND (c.conrelid = 'request_logs'::regclass
           OR c.conrelid IN (SELECT inhrelid FROM pg_inherits WHERE inhparent = 'request_logs'::regclass))
"""


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


def _fk_actions(conn):
    """``{relation: {column: confdeltype}}`` for request_logs and every chunk."""
    actions = {}
    for rel, col, action in conn.execute(text(_FK_ACTIONS)).all():
        actions.setdefault(rel, {})[col] = action
    return actions


def _assert_all_fks(conn, action, columns=tuple(_FKS)):
    actions = _fk_actions(conn)
    chunks = set(conn.execute(text("SELECT show_chunks('request_logs')::text")).scalars())
    assert chunks, "request_logs has no chunks, so the chunk half of this check would prove nothing"
    assert set(actions) == {"request_logs"} | chunks
    for rel, by_column in actions.items():
        assert by_column == dict.fromkeys(columns, action), rel


def _chunk_of_row(conn):
    """The chunk holding the seeded row, and whether it is compressed."""
    return conn.execute(text(
        "SELECT r.tableoid::regclass::text, ch.is_compressed "
        "FROM request_logs r "
        "JOIN timescaledb_information.chunks ch "
        "  ON format('%I.%I', ch.chunk_schema, ch.chunk_name)::regclass = r.tableoid "
        "WHERE r.source = :s"
    ), {"s": _SOURCE}).one()


def _row_ids(conn, columns=tuple(_FKS)):
    return conn.execute(text(
        f"SELECT {', '.join(columns)} FROM request_logs WHERE source = :s"
    ), {"s": _SOURCE}).all()


@pytest.fixture(scope="module")
def history(pg_migrated_isolated):
    """One request_logs row in a compressed chunk, referencing all four parents.

    Yields ``{column: parent id}`` for the row's entity, model, endpoint and API key.
    """
    _, engine = pg_migrated_isolated
    with engine.begin() as conn:
        entity = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'fk-restrict-user', 'FR', true) RETURNING id"
        )).scalar()
        model, endpoint_model = (
            conn.execute(text(
                "INSERT INTO model_configs (model_name, input_cost_per_million, output_cost_per_million) "
                "VALUES (:n, 1, 1) RETURNING id"
            ), {"n": f"fk-restrict-{role}"}).scalar()
            for role in ("logged", "endpoint-owner")
        )
        endpoint = conn.execute(text(
            "INSERT INTO model_endpoints (model_config_id, url, api_key, healthy) "
            "VALUES (:m, 'https://fk-restrict.example/v1', 'k', true) RETURNING id"
        ), {"m": endpoint_model}).scalar()
        api_key = conn.execute(text(
            "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
            " output_tokens, audio_seconds, cost, created_at)"
            " VALUES (:e, 'fk-restrict', 'fk-restrict-logged', 0, 0, 0, 0, 0, now()) RETURNING id"
        ), {"e": entity}).scalar()
        # A month back lands in a closed 7-day chunk, which can be compressed.
        conn.execute(text(
            "INSERT INTO request_logs (time, entity_id, model_config_id, model_endpoint_id, api_key_id, source) "
            "VALUES (now() - INTERVAL '30 days', :e, :m, :ep, :k, :s)"
        ), {"e": entity, "m": model, "ep": endpoint, "k": api_key, "s": _SOURCE})
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "SELECT compress_chunk(c, if_not_compressed => true) "
            "FROM show_chunks('request_logs', older_than => INTERVAL '7 days') c"
        ))
    with engine.connect() as conn:
        _, compressed = _chunk_of_row(conn)
    assert compressed, "the seeded row's chunk is not compressed, so this module would prove nothing"
    return {"entity_id": entity, "model_config_id": model, "model_endpoint_id": endpoint, "api_key_id": api_key}


def test_every_request_logs_fk_restricts_at_head(pg_migrated_isolated, history):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        _assert_all_fks(conn, "r")


@pytest.mark.parametrize("column", list(_FKS))
def test_deleting_a_parent_with_compressed_history_is_refused(pg_migrated_isolated, history, column):
    _, engine = pg_migrated_isolated
    constraint, parent = _FKS[column]
    with engine.connect() as conn:
        chunk_before = _chunk_of_row(conn)

    with pytest.raises(IntegrityError, match=constraint):
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {parent} WHERE id = :id"), {"id": history[column]})

    with engine.connect() as conn:
        assert _chunk_of_row(conn) == chunk_before
        assert _row_ids(conn) == [tuple(history[c] for c in _FKS)]
        assert conn.execute(
            text(f"SELECT count(*) FROM {parent} WHERE id = :id"), {"id": history[column]}
        ).scalar() == 1


def test_parents_without_history_can_still_be_deleted(pg_migrated_isolated, history):
    """Erasure (#378) relies on this: once the logs are moved, the delete goes
    through and the entity's conversations and messages cascade away."""
    _, engine = pg_migrated_isolated
    with engine.begin() as conn:
        entity = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', 'fk-restrict-unlogged', 'FU', true) RETURNING id"
        )).scalar()
        conversation = conn.execute(text(
            "INSERT INTO conversations (entity_id) VALUES (:e) RETURNING id"
        ), {"e": entity}).scalar()
        conn.execute(text(
            "INSERT INTO messages (conversation_id, role, content) VALUES (:c, 'user', 'hello')"
        ), {"c": conversation})
        model = conn.execute(text(
            "INSERT INTO model_configs (model_name, input_cost_per_million, output_cost_per_million) "
            "VALUES ('fk-restrict-unlogged', 1, 1) RETURNING id"
        )).scalar()
        endpoint = conn.execute(text(
            "INSERT INTO model_endpoints (model_config_id, url, api_key, healthy) "
            "VALUES (:m, 'https://fk-restrict-unlogged.example/v1', 'k', true) RETURNING id"
        ), {"m": model}).scalar()
        api_key = conn.execute(text(
            "INSERT INTO api_keys (entity_id, name, key_hash, requests, input_tokens,"
            " output_tokens, audio_seconds, cost, created_at)"
            " VALUES (:e, 'fk-restrict', 'fk-restrict-unlogged', 0, 0, 0, 0, 0, now()) RETURNING id"
        ), {"e": entity}).scalar()

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM api_keys WHERE id = :id"), {"id": api_key})
        conn.execute(text("DELETE FROM model_endpoints WHERE id = :id"), {"id": endpoint})
        conn.execute(text("DELETE FROM model_configs WHERE id = :id"), {"id": model})
        conn.execute(text("DELETE FROM entities WHERE id = :id"), {"id": entity})

    with engine.connect() as conn:
        for table, id_ in (
            ("api_keys", api_key),
            ("model_endpoints", endpoint),
            ("model_configs", model),
            ("entities", entity),
            ("conversations", conversation),
        ):
            assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": id_}).scalar() == 0, table
        assert conn.execute(
            text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": conversation}
        ).scalar() == 0
        assert _row_ids(conn) == [tuple(history[c] for c in _FKS)]


def _fk_and_comment_diffs(conn):
    """Autogenerate diffs for the request_logs FKs, the FK columns' comments
    and the table comment.

    Comments on the other columns are left out on purpose: the timing columns
    (``started_at`` .. ``outcome``) already drift from the model, which is not
    this revision's concern.
    """
    diffs = []
    for diff in compare_metadata(MigrationContext.configure(conn), RequestLog.metadata):
        # Column-level diffs arrive as a list of tuples; table-level ones as a tuple.
        for item in diff if isinstance(diff, list) else [diff]:
            kind = item[0]
            if kind in ("add_fk", "remove_fk") and item[1].parent.name == "request_logs":
                diffs.append(item)
            elif kind == "modify_comment" and item[2] == "request_logs" and item[3] in _FKS:
                diffs.append(item)
            elif kind in ("add_table_comment", "remove_table_comment") and item[1].name == "request_logs":
                diffs.append(item)
    return diffs


def test_autogenerate_proposes_no_fk_or_comment_changes(pg_migrated_isolated, history):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        assert _fk_and_comment_diffs(conn) == []


def test_round_trip_over_a_compressed_chunk(pg_migrated_isolated, history):
    url, engine = pg_migrated_isolated
    expected_row = [tuple(history[c] for c in _REVISION_FKS)]
    engine.dispose()

    # Below this revision api_key_id does not exist yet: the downgrade drops it
    # (c5d7e9f1a3b6), so only the three FKs this revision switches are compared.
    _flask_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        _assert_all_fks(conn, "n", _REVISION_FKS)
        # The comparison has teeth: below the revision it sees the old FKs and comments.
        diffs = _fk_and_comment_diffs(conn)
        assert {d[0] for d in diffs} >= {"add_fk", "remove_fk", "modify_comment"}
        assert _row_ids(conn, _REVISION_FKS) == expected_row
        assert _chunk_of_row(conn)[1]
    engine.dispose()

    _flask_db(url, "upgrade")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _HEAD
        _assert_all_fks(conn, "r")
        assert _fk_and_comment_diffs(conn) == []
        assert _row_ids(conn, _REVISION_FKS) == expected_row
        # The column came back empty: the downgrade dropped the key ids.
        assert _row_ids(conn, ("api_key_id",)) == [(None,)]
        assert _chunk_of_row(conn)[1]
