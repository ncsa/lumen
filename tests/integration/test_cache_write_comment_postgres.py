"""The cache-write column comment revision on real PostgreSQL + TimescaleDB.

Upgraded, model_configs.cache_write_cost_per_million documents that NULL means
0; downgraded to 7456ebd042ef it carries the old "same as input" comment again.
The column stays nullable either way.
"""

import pytest
from sqlalchemy import text

from .test_api_key_cache_tokens_postgres import _flask_db

pytestmark = pytest.mark.postgres

REVISION = "2ea58823bdc7"
PREVIOUS = "7456ebd042ef"


def _column(conn):
    return conn.execute(text(
        "SELECT col_description('model_configs'::regclass, a.attnum), NOT a.attnotnull"
        " FROM pg_attribute a"
        " WHERE a.attrelid = 'model_configs'::regclass"
        " AND a.attname = 'cache_write_cost_per_million'"
    )).one()


def test_upgrade_documents_null_as_zero_and_downgrade_restores_it(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    with engine.connect() as conn:
        comment, nullable = _column(conn)
    assert comment == "USD cost per one million cache-write input tokens; NULL = 0 (cache writes are free)"
    assert nullable

    _flask_db(url, "downgrade", PREVIOUS)
    with engine.connect() as conn:
        comment, nullable = _column(conn)
    assert comment == (
        "USD cost per one million cache-write input tokens; NULL = same as input_cost_per_million"
    )
    assert nullable

    _flask_db(url, "upgrade", REVISION)
    with engine.connect() as conn:
        assert _column(conn)[0].endswith("NULL = 0 (cache writes are free)")
