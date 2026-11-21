"""create_app's startup checks and the branches only a tailored config reaches.

Each test writes its own config.yaml, points ``Config`` at it and at a SQLite
file in ``tmp_path``, and builds a throwaway app. The conftest's autouse
fixture disposes the engines of every app built here.
"""
import logging
import sys
import threading
import types
from http import HTTPStatus

import click
import pytest
import yaml

from lumen import _loaded_by_non_serving_cli, _observe_rejection_quietly, _ThemeLoader
from tests.conftest import TEST_CONFIG, no_snapshot_refresher


def _base_config():
    with open(TEST_CONFIG) as f:
        return yaml.safe_load(f)


def _build(tmp_path, monkeypatch, data, *, write=True, record_workers=False):
    """create_app() against ``data`` written as config.yaml (or no file at all).

    With ``record_workers`` the background-worker starters are replaced by
    recorders, and the returned app carries the names started in
    ``app.extensions["started_workers"]``.
    """
    from config import Config

    cfg = tmp_path / "config.yaml"
    if write:
        cfg.write_text(yaml.safe_dump(data))
    # Config reads CONFIG_YAML and DATABASE_URL into class attributes at import
    # time, so patch the attributes rather than the environment.
    monkeypatch.setattr(Config, "CONFIG_YAML", str(cfg))
    monkeypatch.setattr(Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'startup.db'}")
    no_snapshot_refresher(monkeypatch)
    started = _record_workers(monkeypatch) if record_workers else None
    from lumen import create_app
    app = create_app()
    app.extensions["started_workers"] = started
    return app


# The workers create_app starts, as (module, function) pairs.
_WORKERS = [
    ("lumen.services.health", "start_health_checker"),
    ("lumen.services.token_refill", "start_coin_refiller"),
    ("lumen.services.config_watcher", "start_config_watcher"),
    ("lumen.blueprints.oauth.routes", "start_auth_request_janitor"),
    ("lumen.services.key_expiry", "start_key_expirer"),
    ("lumen.services.stats_fold", "start_fold_thread"),
]


def _record_workers(monkeypatch):
    """Replace every background-worker starter; return the names started."""
    import importlib

    started = []
    for module_name, func in _WORKERS:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, func, lambda *args, _f=func: started.append(_f))
    return started


def test_missing_config_yaml_exits(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _build(tmp_path, monkeypatch, None, write=False)
    assert exc.value.code == 1
    assert "config.yaml not found" in capsys.readouterr().err


def test_config_without_active_models_exits(tmp_path, monkeypatch, capsys):
    data = _base_config()
    data["models"] = [{**data["models"][0], "disabled": True}]
    with pytest.raises(SystemExit) as exc:
        _build(tmp_path, monkeypatch, data)
    assert exc.value.code == 1
    assert "no active models" in capsys.readouterr().err


def test_prometheus_without_token_is_disabled(tmp_path, monkeypatch, caplog):
    data = _base_config()
    data["api"] = {"prometheus": {"enabled": True}}
    with caplog.at_level(logging.ERROR):
        app = _build(tmp_path, monkeypatch, data)
    assert any("api.prometheus.token is not set" in r.getMessage() for r in caplog.records)
    assert app.config["YAML_DATA"]["api"]["prometheus"]["enabled"] is False
    with app.test_client() as client:
        assert client.get("/metrics").status_code == HTTPStatus.NOT_FOUND


def test_dev_user_in_production_mode_exits(tmp_path, monkeypatch, caplog):
    data = _base_config()
    data["app"]["debug"] = False
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        _build(tmp_path, monkeypatch, data)
    assert exc.value.code == 1
    assert any("DEV_USER must not be used in production" in r.getMessage() for r in caplog.records)


def test_neither_oauth_nor_dev_user_exits(tmp_path, monkeypatch, caplog):
    data = _base_config()
    del data["app"]["dev_user"]
    for key in ("CLIENT_ID", "CLIENT_SECRET", "SERVER_METADATA_URL", "SCOPES"):
        monkeypatch.delenv(f"OAUTH2_{key}", raising=False)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        _build(tmp_path, monkeypatch, data)
    assert exc.value.code == 1
    assert any("Neither oauth2.client_id nor app.dev_user" in r.getMessage() for r in caplog.records)


def test_oauth_settings_come_from_yaml_and_env_overrides_them(tmp_path, monkeypatch):
    from lumen.extensions import oauth

    # Registering the provider mutates the process-wide OAuth object; give it
    # fresh registries so the session app is left as it was.
    monkeypatch.setattr(oauth, "_registry", {})
    monkeypatch.setattr(oauth, "_clients", {})
    for key in ("CLIENT_SECRET", "SERVER_METADATA_URL", "SCOPES"):
        monkeypatch.delenv(f"OAUTH2_{key}", raising=False)
    monkeypatch.setenv("OAUTH2_CLIENT_ID", "id-from-env")
    data = _base_config()
    del data["app"]["dev_user"]
    data["oauth2"] = {
        "client_id": "id-from-yaml",
        "client_secret": "secret-from-yaml",
        "server_metadata_url": "https://idp.example/.well-known/openid-configuration",
        "scopes": "openid email",
    }

    app = _build(tmp_path, monkeypatch, data)

    assert app.config["OAUTH2_CLIENT_ID"] == "id-from-env"
    assert app.config["OAUTH2_CLIENT_SECRET"] == "secret-from-yaml"
    assert app.config["OAUTH2_SCOPES"] == "openid email"
    assert "provider" in oauth._registry


def test_yaml_database_url_is_used_without_database_url_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data = _base_config()
    yaml_db = f"sqlite:///{tmp_path / 'from-yaml.db'}"
    data["app"]["database"] = {"url": yaml_db}
    app = _build(tmp_path, monkeypatch, data)
    assert app.config["SQLALCHEMY_DATABASE_URI"] == yaml_db


def test_unknown_theme_falls_back_to_default(tmp_path, monkeypatch):
    data = _base_config()
    data["app"]["theme"] = "no-such-theme"
    app = _build(tmp_path, monkeypatch, data)
    assert app.config["THEME_NAME"] == "default"


def test_werkzeug_reloader_child_starts_the_background_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    app = _build(tmp_path, monkeypatch, _base_config(), record_workers=True)
    assert app.extensions["started_workers"] == [func for _, func in _WORKERS]


def test_werkzeug_reloader_parent_starts_no_workers(tmp_path, monkeypatch):
    # The reloader's parent process: WERKZEUG_RUN_MAIN is set but not "true".
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "false")
    monkeypatch.setenv("BACKGROUND_WORKER", "true")
    app = _build(tmp_path, monkeypatch, _base_config(), record_workers=True)
    assert app.extensions["started_workers"] == []


def test_flask_cli_command_starts_no_workers(tmp_path, monkeypatch):
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.setenv("BACKGROUND_WORKER", "true")
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    with click.Context(click.Command("upgrade"), info_name="upgrade"):
        app = _build(tmp_path, monkeypatch, _base_config(), record_workers=True)
    assert app.extensions["started_workers"] == []


def test_flask_cli_without_click_context_is_a_serving_process(monkeypatch):
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    assert click.get_current_context(silent=True) is None
    assert _loaded_by_non_serving_cli() is False


def test_server_errors_render_json_for_the_api_and_html_otherwise(tmp_path, monkeypatch):
    app = _build(tmp_path, monkeypatch, _base_config())
    app.config["PROPAGATE_EXCEPTIONS"] = False

    def boom():
        raise RuntimeError("boom")

    app.add_url_rule("/v1/startup-test-boom", "api_boom", boom)
    app.add_url_rule("/startup-test-boom", "page_boom", boom)

    with app.test_client() as client:
        api = client.get("/v1/startup-test-boom")
        page = client.get("/startup-test-boom")

    assert api.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert api.get_json()["error"]["code"] == "internal_error"
    assert page.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert page.mimetype == "text/html"


def test_healthz_reports_an_unreachable_database(app, client):
    from lumen.extensions import db

    def unreachable(*args, **kwargs):
        raise ConnectionError("database down")

    # Scoped to the request: the suite's clean_db teardown needs the real execute.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db.session, "execute", unreachable)
        status = client.get("/healthz").status_code
    assert status == HTTPStatus.SERVICE_UNAVAILABLE


def test_observe_rejection_is_a_noop_before_the_middleware_is_imported(monkeypatch):
    monkeypatch.delitem(sys.modules, "lumen.blueprints.metrics.middleware", raising=False)
    _observe_rejection_quietly("rate_limit", "api", "")
    assert "lumen.blueprints.metrics.middleware" not in sys.modules


def test_observe_rejection_swallows_instrumentation_errors(monkeypatch):
    calls = []

    def broken(reason, source, model):
        calls.append((reason, source, model))
        raise ValueError("counter unavailable")

    fake = types.SimpleNamespace(observe_rejection=broken)
    monkeypatch.setitem(sys.modules, "lumen.blueprints.metrics.middleware", fake)
    _observe_rejection_quietly("rate_limit", "chat", "m")
    assert calls == [("rate_limit", "chat", "m")]


def test_theme_loader_outside_a_request_uses_the_configured_theme(app):
    """Outside an app context there is no ``g``; the loader uses THEME_NAME."""
    loader = _ThemeLoader(app.config["THEMES_ROOT"], app)
    result = {}

    def load():
        # A fresh thread starts with no app context, whatever this one has pushed.
        source, path, uptodate = loader.get_source(None, "theme/header.html")
        result.update(source=source, path=path, uptodate=uptodate())

    worker = threading.Thread(target=load)
    worker.start()
    worker.join()
    assert result["path"].endswith("default/templates/theme/header.html")
    assert result["source"]
    assert result["uptodate"] is True


def test_text_model_with_empty_output_stays_a_chat_model_at_startup(tmp_path, monkeypatch):
    """input ["text"] with output [] is ambiguous: older Update wrote it for an
    embedding backend, and models.dev sync can write it for a chat model. The
    startup YAML sync keeps it unknown, so a chat model is never hidden; only a
    backend re-sync or an explicit marker makes a model an embedding model."""
    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig, is_embedding_model
    from lumen.services import model_cache

    data = _base_config()
    first = _build(tmp_path, monkeypatch, data)
    with first.app_context():
        db.create_all()

    endpoint = [{"url": "http://localhost:9999/v1", "api_key": "k"}]
    data["models"] = [
        {"name": "text-chat", "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
         "input_modalities": ["text"], "output_modalities": [], "endpoints": endpoint},
        {"name": "marked-embed", "input_cost_per_million": 0.1, "output_cost_per_million": 0,
         "input_modalities": ["text"], "output_modalities": ["embedding"], "endpoints": endpoint},
    ]
    app = _build(tmp_path, monkeypatch, data)

    with app.app_context():
        rows = {mc.model_name: mc for mc in db.session.execute(select(ModelConfig)).scalars()}
        assert rows["text-chat"].output_modalities is None
        assert rows["marked-embed"].output_modalities == ["embedding"]
        model_cache.clear()
        assert not is_embedding_model(model_cache.resolve("text-chat"))
        assert is_embedding_model(model_cache.resolve("marked-embed"))
    model_cache.clear()
