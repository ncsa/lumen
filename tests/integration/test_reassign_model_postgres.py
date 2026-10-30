"""``flask reassign-model`` against compressed request_logs and real aggregates.

``model_config_id`` is a compression segmentby column, so moving a model's history
decompresses every batch of it, and past
``timescaledb.max_tuples_decompressed_per_dml_transaction`` (100,000 by default)
the old single UPDATE aborted the whole command (#174). The aggregates' policies
only reprocess their own short windows, so even a successful move left every older
bucket under the old model.

As in ``test_endpoint_retirement_postgres.py``, the guard is lowered to 10 at
database level and the history is seeded into a genuinely compressed chunk, so a
move that is not split per chunk with the limit lifted aborts here on 60 rows
instead of in production on 100k. The control UPDATE proves the guard is armed.
Assertions on the aggregates read their materialisation hypertables, never the
views, for the reason ``check-aggregate`` gives.

The command runs in-process (not as a subprocess like the lifecycle tests) so the
resumability tests can make one chunk, the refresh or a recompress fail on cue.
Each test uses its own pair of models, so tests sharing the module database only
share chunks, and every test leaves those fully compressed.
"""

import os
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, exc, text

from lumen import commands
from lumen.commands import reassign_model_cmd
from tests.conftest import no_snapshot_refresher

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

OLD_ROWS = 60  # well above the lowered guard (10)
RECENT_ROWS = 3
AGGREGATES = ("request_counts_hourly", "request_counts_hourly_by_entity", "request_metrics_1m")
# request_metrics_1m keeps 30 days (5006b6594383); its older buckets are never refreshed.
RETENTION = {"request_metrics_1m": "30 days"}


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A Flask app on this module's database, with the decompression guard at 10.

    Lowered at database level before the app opens a connection, so every pooled
    connection — the command's and the control UPDATE's — starts with it.
    """
    url = pg_migrated_isolated[0]
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'ALTER DATABASE "{url.rsplit("/", 1)[1]}" SET timescaledb.max_tuples_decompressed_per_dml_transaction = 10'))
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


@pytest.fixture
def engine(pg_app):
    with pg_app.app_context():
        from lumen.extensions import db

        yield db.engine


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _models(engine):
    """A fresh (FROM, TO) pair of model_configs ids."""
    tag = uuid.uuid4().hex[:8]
    with engine.begin() as conn:
        return tuple(
            conn.execute(text(
                "INSERT INTO model_configs (model_name, input_cost_per_million, output_cost_per_million) "
                "VALUES (:n, 1, 1) RETURNING id"
            ), {"n": f"reassign-{tag}-{role}"}).scalar()
            for role in ("from", "to")
        )


def _seed(engine, mcid, age, rows):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, model_config_id, source, input_tokens, output_tokens, cost, duration) "
            f"SELECT now() - INTERVAL '{age}', :m, 'api', 1, 1, 0.01, 0.1 FROM generate_series(1, :r)"
        ), {"m": mcid, "r": rows})


def _seed_history(engine, from_id):
    """60 FROM rows 40 days old in a compressed chunk, a few recent ones, aggregates backfilled."""
    _seed(engine, from_id, "40 days", OLD_ROWS)
    _seed(engine, from_id, "2 days", RECENT_ROWS)
    with _autocommit(engine) as conn:
        # recompress => true: an earlier test may have compressed this chunk already,
        # and rows inserted since sit in its heap.
        conn.execute(text(
            "SELECT compress_chunk(c, recompress => true) "
            "FROM show_chunks('request_logs', older_than => INTERVAL '7 days') c"
        ))
        for view in AGGREGATES:
            conn.execute(text(f"CALL refresh_continuous_aggregate('{view}', now() - INTERVAL '60 days', now())"))
    assert _compressed_chunks(engine), "nothing was compressed; this test would prove nothing about compressed history"
    assert _materialised(engine, "request_counts_hourly_by_entity", from_id) == OLD_ROWS + RECENT_ROWS


def _compressed_chunks(engine):
    """``{chunk: rows in its uncompressed heap}`` for every chunk flagged compressed."""
    with engine.connect() as conn:
        chunks = conn.execute(text(
            "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'request_logs' AND is_compressed"
        )).scalars().all()
        return {c: conn.execute(text(f"SELECT count(*) FROM ONLY {c}")).scalar() for c in chunks}


def _since(view):
    return f"now() - INTERVAL '{RETENTION[view]}'" if view in RETENTION else "'-infinity'"


def _materialised(engine, view, mcid):
    """Requests materialised under ``mcid`` in ``view``, inside its retention."""
    with engine.connect() as conn:
        table = conn.execute(text(
            "SELECT format('%I.%I', materialization_hypertable_schema, materialization_hypertable_name) "
            "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
        ), {"v": view}).scalar()
        return conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {table} "
            f"WHERE model_config_id = :m AND bucket >= {_since(view)}"
        ), {"m": mcid}).scalar()


def _raw(engine, mcid, view=None):
    with engine.connect() as conn:
        return conn.execute(text(
            f"SELECT count(*) FROM request_logs WHERE model_config_id = :m AND time >= {_since(view)}"
        ), {"m": mcid}).scalar()


def _run(pg_app, from_id, to_id):
    return pg_app.test_cli_runner().invoke(reassign_model_cmd, [str(from_id), str(to_id)])


def _assert_fully_moved(engine, from_id, to_id):
    assert _raw(engine, from_id) == 0
    assert _raw(engine, to_id) == OLD_ROWS + RECENT_ROWS
    for view in AGGREGATES:
        assert _materialised(engine, view, from_id) == 0, f"{view} still has buckets under FROM"
        assert _materialised(engine, view, to_id) == _raw(engine, to_id, view), f"{view} totals under TO do not match raw"
    assert set(_compressed_chunks(engine).values()) <= {0}, "a compressed chunk was left with rows in its heap"


def test_moves_compressed_history_and_refreshes_aggregates(pg_app, engine):
    from_id, to_id = _models(engine)
    _seed_history(engine, from_id)

    result = _run(pg_app, from_id, to_id)

    assert result.exit_code == 0, result.output
    assert f"request_logs updated: {OLD_ROWS + RECENT_ROWS}" in result.output
    assert ": recompressed" in result.output
    _assert_fully_moved(engine, from_id, to_id)
    # The old buckets were materialised under FROM by the backfill; only an explicit
    # refresh reaching 40 days back moves them.
    assert _materialised(engine, "request_counts_hourly", to_id) == OLD_ROWS + RECENT_ROWS

    # Guard leak: the lifted limit was SET LOCAL, so a pooled connection still has 10.
    with engine.connect() as conn:
        assert conn.execute(text("SHOW timescaledb.max_tuples_decompressed_per_dml_transaction")).scalar() == "10"

    # Control: the old unsplit UPDATE aborts against the guard. If it stops raising,
    # the guard is not enforced and the move above proved nothing.
    with pytest.raises(exc.DBAPIError) as aborted:
        with engine.begin() as conn:
            conn.execute(text("UPDATE request_logs SET model_config_id = :f WHERE model_config_id = :t"),
                         {"f": from_id, "t": to_id})
    assert "decompression limit" in str(aborted.value), f"the control UPDATE failed with something else: {aborted.value}"


def test_rerun_and_no_request_logs_skip_the_refresh(pg_app, engine):
    from lumen.extensions import db
    from lumen.models.conversation import Conversation
    from lumen.models.entity import Entity
    from lumen.models.model_stat import ModelStat

    from_id, to_id = _models(engine)
    _seed_history(engine, from_id)
    assert _run(pg_app, from_id, to_id).exit_code == 0

    result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 0, result.output
    assert "no request_logs rows for FROM; aggregates unchanged" in result.output
    assert "conversations updated: 0" in result.output
    assert "model_stats moved: 0, merged: 0" in result.output
    _assert_fully_moved(engine, from_id, to_id)

    # A FROM with conversations and stats but no request_logs at all.
    from_id, to_id = _models(engine)
    with pg_app.app_context():
        from_name = db.session.get(commands.ModelConfig, from_id).model_name
        user = Entity(entity_type="user", email=f"{from_name}@example.com", name="Reassign", active=True)
        db.session.add(user)
        db.session.flush()
        db.session.add_all([Conversation(entity_id=user.id, model=from_name),
                            ModelStat(entity_id=user.id, model_config_id=from_id, source="chat", requests=1)])
        db.session.commit()

    result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 0, result.output
    assert "no request_logs rows for FROM; aggregates unchanged" in result.output
    assert "conversations updated: 1" in result.output
    assert "model_stats moved: 1, merged: 0" in result.output


def test_resumes_after_a_chunk_fails(pg_app, engine):
    """The second chunk's UPDATE fails: exit 1 naming the chunks done; a re-run finishes,
    including the refresh of the months the first run moved."""
    from_id, to_id = _models(engine)
    _seed_history(engine, from_id)
    real = commands._move_chunk
    calls = []

    def fail_second(*args):
        calls.append(args)
        if len(calls) == 2:
            raise exc.DBAPIError("UPDATE request_logs", {}, Exception("injected"))
        return real(*args)

    with patch.object(commands, "_move_chunk", side_effect=fail_second):
        result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 1, result.output
    assert "Chunks done: " in result.output
    assert "re-run" in result.output
    assert 0 < _raw(engine, from_id) < OLD_ROWS + RECENT_ROWS, "the failure did not land between the two chunks"

    result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 0, result.output
    _assert_fully_moved(engine, from_id, to_id)


def test_resumes_after_the_refresh_fails(pg_app, engine):
    """Everything moved but no aggregate refreshed: the re-run finds FROM only in the
    materialised buckets and refreshes from the oldest of them."""
    from_id, to_id = _models(engine)
    _seed_history(engine, from_id)

    def fail(conn, name, start, now):
        raise commands._RefreshFailed(start, [], exc.DBAPIError("CALL", {}, Exception("injected")))

    with patch.object(commands, "_refresh_months", side_effect=fail):
        result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 1, result.output
    assert _raw(engine, from_id) == 0
    assert _materialised(engine, "request_counts_hourly", from_id) == OLD_ROWS + RECENT_ROWS

    result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 0, result.output
    assert "no request_logs rows" not in result.output
    _assert_fully_moved(engine, from_id, to_id)


def test_rerun_recompresses_a_chunk_left_half_done(pg_app, engine):
    """The recompress after the first chunk never happened (Ctrl-C between the two):
    a plain re-run, with nothing left to move, recompresses it."""
    from_id, to_id = _models(engine)
    _seed_history(engine, from_id)
    real = commands._recompress_chunk
    skipped = []

    def skip_first(conn, chunk):
        if not skipped:
            skipped.append(chunk)
            return False
        return real(conn, chunk)

    with patch.object(commands, "_recompress_chunk", side_effect=skip_first):
        assert _run(pg_app, from_id, to_id).exit_code == 0
    assert _compressed_chunks(engine)[skipped[0]] > 0, "the skipped chunk has no heap rows; this test would prove nothing"

    result = _run(pg_app, from_id, to_id)
    assert result.exit_code == 0, result.output
    assert f"{skipped[0]}: recompressed" in result.output
    _assert_fully_moved(engine, from_id, to_id)


def test_refuses_with_a_retention_policy_on_request_logs(pg_app, engine):
    from_id, to_id = _models(engine)
    _seed(engine, from_id, "2 days", 1)
    with _autocommit(engine) as conn:
        conn.execute(text("SELECT add_retention_policy('request_logs', INTERVAL '400 days')"))
    try:
        result = _run(pg_app, from_id, to_id)
    finally:
        with _autocommit(engine) as conn:
            conn.execute(text("SELECT remove_retention_policy('request_logs')"))

    assert result.exit_code == 1, result.output
    assert "flask check-aggregate" in result.output
    assert _raw(engine, from_id) == 1, "rows moved before the refusal"
