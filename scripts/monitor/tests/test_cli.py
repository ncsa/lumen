import logging
import runpy
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from monitor import cli
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


@pytest.mark.parametrize("stopped, code", [(True, 0), (False, 1)])
def test_serve_runs_the_loops_and_dashboard_then_disposes_the_engine(tmp_path, monkeypatch, stopped, code):
    monkeypatch.setenv("MONITOR_DATABASE_URL", f"sqlite:///{tmp_path / 'monitor.db'}")
    path = tmp_path / "config.yaml"
    path.write_text("lumen: {url: 'http://lumen.test'}\n")
    engines, servers, served = [], [], []
    real_make_engine = cli.make_engine

    def make_engine(url):
        engines.append(real_make_engine(url))
        return engines[-1]

    def make_server(host, port, app, threaded):
        servers.append((host, port, threaded))
        return "server"

    def serve(scheduler, server):
        served.append((scheduler, server))
        return stopped

    monkeypatch.setattr(cli, "make_engine", make_engine)
    monkeypatch.setattr(cli, "make_server", make_server)
    monkeypatch.setattr(cli, "serve", serve)
    disposed = []
    real_dispose = Engine.dispose

    def dispose(self, *args, **kwargs):
        disposed.append(self)
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "dispose", dispose)

    assert main(["--config", str(path), "serve", "--host", "127.0.0.1", "--port", "5099"]) == code
    assert servers == [("127.0.0.1", 5099, True)]
    assert len(served) == 1 and served[0][1] == "server"
    assert engines and disposed == engines


def test_collect_needs_a_collector(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(tmp_path / "missing.yaml"), "collect"])
    assert exc.value.code == 2
    assert "choose a collector" in capsys.readouterr().err


def test_module_runs_main_as_a_script(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["monitor", "--config", str(tmp_path / "missing.yaml"), "collect", "--probe"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(cli.__file__, run_name="__main__")
    assert exc.value.code == 2
