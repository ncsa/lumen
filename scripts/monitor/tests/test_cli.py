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
