import pytest
from alembic import command
from sqlalchemy import inspect

from monitor.db import make_engine
from monitor.migrate import alembic_config, downgrade, upgrade

TABLES = {"daily_stats", "probes", "samples"}


def _tables(url):
    engine = make_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def _round_trip(url):
    upgrade(url)
    assert _tables(url) == TABLES
    # The models and the migrations describe the same schema.
    command.check(alembic_config(url))
    downgrade(url, "base")
    assert _tables(url) == set()
    upgrade(url)
    assert _tables(url) == TABLES


def test_sqlite_upgrade_downgrade(tmp_path):
    _round_trip(f"sqlite:///{tmp_path / 'monitor.db'}")


@pytest.mark.postgres
def test_postgres_upgrade_downgrade(postgres_url):
    try:
        _round_trip(postgres_url)
    finally:
        downgrade(postgres_url, "base")
