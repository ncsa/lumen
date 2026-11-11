"""The drop_redundant_indexes revision on real PostgreSQL + TimescaleDB.

The SQLite suite builds the schema with ``create_all``, and TimescaleDB's
default ``request_logs_time_idx`` only exists on a hypertable, so the index set
an upgraded database ends up with, and whether autogenerate agrees with the
models, is only provable here. The round trip over a compressed chunk steps
this module's private database back below the revision and up again, so it
runs against ``pg_migrated_isolated`` and leaves it at head.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from lumen.models import RequestLog
from tests.integration.conftest import migrate_db
from tests.unit.test_migrations import _script_dir

pytestmark = pytest.mark.postgres

_BEFORE = "a6b8c0d2e4f6"
_REVISION = "b7c9d1e3f5a8"
_DROPPED = {"ix_request_logs_time", "ix_request_logs_model_config_id", "ix_model_stats_entity_id"}
_REQUEST_LOGS_INDEXES = {
    "request_logs_time_idx",
    "ix_request_logs_entity_id",
    "ix_request_logs_model_config_id_time",
}
_SOURCE = "redidx"  # request_logs.source is VARCHAR(8)


def _indexes(conn, table):
    return dict(conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
        {"t": table},
    ).all())


def _primary_key(conn, table):
    return conn.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'p'"
    ), {"t": table}).scalar_one()


def _unique_constraints(conn, table):
    return set(conn.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'u'"
    ), {"t": table}).scalars())


def _all_index_names(conn):
    return set(conn.execute(text(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename IN ('request_logs', 'model_stats')"
    )).scalars())


def test_head_keeps_only_the_needed_indexes(pg_migrated_isolated):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        request_logs = _indexes(conn, "request_logs")
        request_logs_pk = _primary_key(conn, "request_logs")
        model_stats = _indexes(conn, "model_stats")
        model_stats_unique = _unique_constraints(conn, "model_stats")

    assert set(request_logs) == {request_logs_pk} | _REQUEST_LOGS_INDEXES
    assert request_logs["request_logs_time_idx"].endswith('("time" DESC)')

    assert "ix_model_stats_entity_id" not in model_stats
    assert "ix_model_stats_model_config_id" in model_stats
    assert len(model_stats_unique) == 1
    assert model_stats_unique <= set(model_stats)


def test_autogenerate_proposes_no_index_changes(pg_migrated_isolated):
    _, engine = pg_migrated_isolated
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), RequestLog.metadata)

    index_diffs = [
        diff for diff in diffs
        if isinstance(diff, tuple) and diff[0] in ("add_index", "remove_index")
        and diff[1].table.name in ("request_logs", "model_stats")
    ]
    assert index_diffs == []


def test_round_trip_over_a_compressed_chunk(pg_migrated_isolated):
    url, engine = pg_migrated_isolated

    # A row a month back lands in a closed 7-day chunk, which can be compressed.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (now() - INTERVAL '30 days', :s, 1, 1, 0.1, 0.1)"
        ), {"s": _SOURCE})
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "SELECT compress_chunk(c, if_not_compressed => true) "
            "FROM show_chunks('request_logs', older_than => INTERVAL '7 days') c"
        ))
        compressed = conn.execute(text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND is_compressed"
        )).scalar()
    assert compressed >= 1, "no chunk was compressed, so this test would prove nothing"
    engine.dispose()

    migrate_db(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        assert _DROPPED <= _all_index_names(conn)
        assert "request_logs_time_idx" in _indexes(conn, "request_logs")
    engine.dispose()

    migrate_db(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        assert not _DROPPED & _all_index_names(conn)
        assert _REQUEST_LOGS_INDEXES <= set(_indexes(conn, "request_logs"))
        rows = conn.execute(text("SELECT count(*) FROM request_logs WHERE source = :s"), {"s": _SOURCE}).scalar()
    assert rows == 1
    engine.dispose()

    migrate_db(url, "upgrade", "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _script_dir().get_current_head()
