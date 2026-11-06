import logging

import pytest
from sqlalchemy import inspect

from monitor.cli import main
from monitor.db import make_engine


def test_migrate_creates_schema(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    assert main(["--config", str(tmp_path / "missing.yaml"), "migrate"]) == 0
    engine = make_engine(url)
    assert {"daily_stats", "probes", "samples"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_bad_config_exits_2(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("refresh: {timeout: 5m}\n")
    assert main(["--config", str(path), "migrate"]) == 2


@pytest.mark.parametrize("command", [["serve"], ["collect", "--probe"], ["collect", "--price"]])
def test_collect_and_serve_require_lumen_url(tmp_path, monkeypatch, caplog, command):
    monkeypatch.setenv("MONITOR_DATABASE_URL", f"sqlite:///{tmp_path / 'monitor.db'}")
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n")
    with caplog.at_level(logging.ERROR, logger="monitor.cli"):
        assert main(["--config", str(path), *command]) == 2
    assert "lumen.url is required" in caplog.text
    assert not (tmp_path / "monitor.db").exists()


def test_migrate_does_not_require_lumen_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n")
    assert main(["--config", str(path), "migrate"]) == 0
    engine = make_engine(url)
    assert "probes" in inspect(engine).get_table_names()
    engine.dispose()
