"""Endpoint retirement must not rewrite compressed request_logs history.

The production failure behind the unit tests in
``tests/unit/test_endpoint_retirement.py``: removing an endpoint from
config.yaml deleted its ``model_endpoints`` row, and the
``request_logs.model_endpoint_id`` ``ON DELETE SET NULL`` action answered
with ``UPDATE ONLY request_logs SET model_endpoint_id = NULL``. Servicing
that UPDATE forces TimescaleDB to decompress every compressed tuple
referencing the endpoint, and past
``timescaledb.max_tuples_decompressed_per_dml_transaction`` (100,000 by
default) the statement aborts — production logged "aborting after
decompressing 114639 tuples against a limit of 100000" — rolling back the
endpoint removal and the whole config sync with it. The app then kept
serving a config that no longer matched the database.

Retirement replaces the delete with ``model_endpoints.active = false``, so
request_logs is never written at all. This module proves that against a
genuinely compressed chunk with the guard deliberately lowered to 10, so
any history rewrite aborts immediately instead of silently succeeding
whenever a test seeds fewer rows than production's 114k. The control
DELETE at the end proves the guard is armed: if a TimescaleDB upgrade ever
stops enforcing it, this module reports that rather than passing vacuously.

No SQLite trigger can see a compressed chunk, which is why the compressed
half of the guarantee lives here and only the behavioural matrix (single
and duplicate URLs, whole-model removal, re-adding) lives in the unit file.
"""

import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, exc, func, select, text

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

A_URL = "https://retired.example/v1"
B_URL = "https://surviving.example/v1"
SEED_ROWS = 60  # well above the lowered guard (10), far below production's 114639

_SYNC_ALL = {
    "models": [
        {
            "name": "retirement-model",
            "input_cost_per_million": 1,
            "output_cost_per_million": 1,
            "endpoints": [{"url": url, "api_key": "test-key"} for url in (A_URL, B_URL)],
        }
    ]
}

_SYNC_B_ONLY = {
    "models": [
        {
            "name": "retirement-model",
            "input_cost_per_million": 1,
            "output_cost_per_million": 1,
            "endpoints": [{"url": B_URL, "api_key": "test-key"}],
        }
    ]
}

# Seeded into a closed chunk (40 days old, chunk interval is 7 days) so the
# compression step below has something to compress. All other request_logs
# columns carry server defaults.
_SEED = """
    INSERT INTO request_logs (time, model_config_id, model_endpoint_id, source)
    SELECT now() - INTERVAL '40 days', :mcid, :epid, 'api'
    FROM generate_series(1, :rows)
"""


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A real Flask app bound to this module's migrated PostgreSQL database.

    Before the app opens a single connection, the per-statement decompression
    guard is lowered at database level: pooled connections inherit database
    settings at connect time, so the sync's session — and the control DELETE —
    must be able to see the lowered limit. The guard is the SQLite trigger of
    the unit tests, made real: any UPDATE that reaches into the compressed
    chunk aborts, which is exactly how production failed.
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

    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        from lumen.extensions import db

        assert db.engine.dialect.name == "postgresql", "the test app is not on PostgreSQL; nothing below would touch a compressed chunk"
        guard = db.session.execute(text("SHOW timescaledb.max_tuples_decompressed_per_dml_transaction")).scalar()
        assert guard == "10", f"the decompression guard is {guard}, not 10; a history rewrite below would silently decompress instead of aborting"
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


def _compress_old_chunks(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('request_logs', older_than => INTERVAL '7 days') c"))


def _compressed_chunks(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'request_logs' AND is_compressed")).scalar()


def test_retirement_preserves_compressed_history(pg_app):
    with pg_app.app_context():
        from lumen.commands import sync_models_from_yaml
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.models.request_log import RequestLog
        from lumen.services.health import check_all_endpoints
        from lumen.services.llm import get_next_endpoint

        sync_models_from_yaml(_SYNC_ALL)
        config = db.session.execute(select(ModelConfig).filter_by(model_name="retirement-model")).scalar_one()
        endpoint_a, endpoint_b = sorted(config.endpoints, key=lambda ep: ep.url)
        a_id, b_id, mcid = endpoint_a.id, endpoint_b.id, config.id

        db.session.execute(text(_SEED), {"mcid": mcid, "epid": a_id, "rows": SEED_ROWS})
        db.session.commit()
        _compress_old_chunks(db.engine)
        compressed_before = _compressed_chunks(db.engine)
        assert compressed_before >= 1, (
            "nothing was actually compressed — the seed landed in the open chunk, so this test proved nothing about compressed history"
        )

        # The production action: a validated config that dropped one of two
        # distinct-URL endpoints. Under the old delete-based sync this is the
        # statement sequence that aborted against compressed chunks.
        sync_models_from_yaml(_SYNC_B_ONLY)

        retired = db.session.get(ModelEndpoint, a_id)
        assert retired is not None, "the retired row was deleted, not retained"
        assert retired.active is False
        assert retired.healthy is False, "retirement must drop stale health"
        assert db.session.get(ModelEndpoint, b_id).active is True

        # History still attributes every request to the endpoint that served it.
        assert db.session.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.model_endpoint_id == a_id)) == SEED_ROWS
        assert db.session.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.model_endpoint_id.is_(None))) == 0

        # And the chunk never had to give up its columnstore: a rewrite would
        # have decompressed it before TimescaleDB aborted the sync.
        assert _compressed_chunks(db.engine) == compressed_before

        # The retired row is excluded from routing and health probing.
        db.session.get(ModelEndpoint, b_id).healthy = True
        db.session.commit()
        for _ in range(3):
            assert get_next_endpoint(mcid).url == B_URL
        with patch("lumen.services.health._probe_endpoint", return_value=True) as probe:
            assert check_all_endpoints() == 1
            assert probe.call_args.args[0] == B_URL

    # Control, in its own transaction: the old delete-based sync must abort
    # against this chunk. If this stops raising, the guard is no longer
    # enforced and the assertions above no longer prove anything.
    with pg_app.app_context():
        from lumen.extensions import db

        with pytest.raises(exc.DBAPIError) as aborted:
            with db.engine.begin() as conn:
                conn.execute(text("DELETE FROM model_endpoints WHERE id = :i"), {"i": a_id})
        assert "decompression limit" in str(aborted.value), f"the control DELETE failed with something else: {aborted.value}"
