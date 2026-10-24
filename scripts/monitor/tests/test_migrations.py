import pytest
from alembic import command
from sqlalchemy import inspect, text

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


def _columns(url, table):
    engine = make_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


SPLIT_DAILY = {"input_tokens", "output_tokens", "input_tokens_covered_until", "output_tokens_covered_until"}
SPLIT_SAMPLES = {"input_tokens_last_minute", "output_tokens_last_minute"}


def test_split_tokens_revision_adds_and_drops_its_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    upgrade(url, "0002")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO daily_stats (day, model, source, last_updated, tokens, estimated, final, polls, polls_ok, healthy_sum) "
            "VALUES ('2026-10-17', 'qwen', 'lumen', '2026-10-17 05:00:00', 1000, 0, 0, 0, 0, 0)"
        ))
    engine.dispose()
    upgrade(url, "0003")
    assert SPLIT_DAILY <= _columns(url, "daily_stats")
    assert SPLIT_SAMPLES <= _columns(url, "samples")
    downgrade(url, "0002")
    assert not SPLIT_DAILY & _columns(url, "daily_stats")
    assert not SPLIT_SAMPLES & _columns(url, "samples")
    engine = make_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT tokens FROM daily_stats WHERE model = 'qwen'")).scalar_one() == 1000
    engine.dispose()
