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


def _check_constraints(url, table):
    engine = make_engine(url)
    try:
        return {check["name"] for check in inspect(engine).get_check_constraints(table)}
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
        # `command.check` does not compare CHECK constraints, so name it here.
        assert "ck_daily_stats_source" in _check_constraints(postgres_url, "daily_stats")
    finally:
        downgrade(postgres_url, "base")


def _columns(url, table):
    engine = make_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


CACHED_INPUT_DAILY = {"cached_input_price"}
CACHE_WRITE_DAILY = {"cache_write_price"}
MODELSDEV_CACHE_DAILY = {f"{kind}_{stat}" for kind in ("cache_read", "cache_write") for stat in ("median", "p5", "p95")}


SPLIT_DAILY = {"input_tokens", "output_tokens", "input_tokens_covered_until", "output_tokens_covered_until"}
SPLIT_SAMPLES = {"input_tokens_last_minute", "output_tokens_last_minute"}
COMBINED_DAILY = {"tokens", "tokens_covered_until"}


def test_split_tokens_revision_swaps_the_combined_columns_for_the_split(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    upgrade(url, "0002")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO daily_stats (day, model, source, last_updated, requests, tokens, estimated, final, polls, polls_ok, healthy_sum) "
            "VALUES ('2026-10-17', 'qwen', 'lumen', '2026-10-17 05:00:00', 10, 1000, 0, 0, 0, 0, 0)"
        ))
    engine.dispose()
    upgrade(url, "0003")
    assert SPLIT_DAILY <= _columns(url, "daily_stats")
    assert SPLIT_SAMPLES <= _columns(url, "samples")
    assert not COMBINED_DAILY & _columns(url, "daily_stats")
    assert "tokens_last_minute" not in _columns(url, "samples")
    downgrade(url, "0002")
    assert not SPLIT_DAILY & _columns(url, "daily_stats")
    assert not SPLIT_SAMPLES & _columns(url, "samples")
    assert COMBINED_DAILY <= _columns(url, "daily_stats")
    assert "tokens_last_minute" in _columns(url, "samples")
    engine = make_engine(url)
    with engine.connect() as conn:
        # The other columns survive; the combined tokens come back empty.
        assert conn.execute(text("SELECT requests, tokens FROM daily_stats WHERE model = 'qwen'")).one() == (10, None)
    engine.dispose()


def test_cached_input_price_revision_adds_and_drops_the_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    upgrade(url, "0003")
    assert not CACHED_INPUT_DAILY & _columns(url, "daily_stats")
    upgrade(url, "0004")
    assert CACHED_INPUT_DAILY <= _columns(url, "daily_stats")
    downgrade(url, "0003")
    assert not CACHED_INPUT_DAILY & _columns(url, "daily_stats")


def test_cache_write_price_revision_adds_and_drops_the_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    upgrade(url, "0004")
    assert not CACHE_WRITE_DAILY & _columns(url, "daily_stats")
    upgrade(url, "0005")
    assert CACHE_WRITE_DAILY <= _columns(url, "daily_stats")
    downgrade(url, "0004")
    assert not CACHE_WRITE_DAILY & _columns(url, "daily_stats")
    assert CACHED_INPUT_DAILY <= _columns(url, "daily_stats")


def test_modelsdev_cache_prices_revision_adds_and_drops_the_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    upgrade(url, "0005")
    assert not MODELSDEV_CACHE_DAILY & _columns(url, "daily_stats")
    upgrade(url, "0006")
    assert MODELSDEV_CACHE_DAILY <= _columns(url, "daily_stats")
    downgrade(url, "0005")
    assert not MODELSDEV_CACHE_DAILY & _columns(url, "daily_stats")
    assert CACHE_WRITE_DAILY <= _columns(url, "daily_stats")
