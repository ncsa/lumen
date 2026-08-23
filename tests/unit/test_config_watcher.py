"""Tests for config_watcher._check_restart_required, _watcher, and start_config_watcher."""
import logging

import pytest


@pytest.fixture
def restore_config(app):
    """Snapshot app.config and restore it afterward.

    apply_hot_config mutates the shared session-scoped app.config; restoring
    prevents leakage into later tests (e.g. CONFIG_EDITOR / DEV_USER).
    """
    saved = dict(app.config)
    yield
    app.config.clear()
    app.config.update(saved)


def _check(old, new, caplog):
    from lumen.services.config_watcher import _check_restart_required
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        _check_restart_required(old, new)


def test_identical_data_no_warnings(caplog):
    data = {"app": {"secret_key": "abc", "database_url": "postgres://x"}}
    _check(data, data, caplog)
    assert caplog.records == []


def test_secret_key_change_warns(caplog):
    _check({"app": {"secret_key": "old"}}, {"app": {"secret_key": "new"}}, caplog)
    messages = " ".join(r.message for r in caplog.records)
    assert "secret_key" in messages
    assert "restart" in messages


def test_database_change_warns(caplog):
    _check(
        {"app": {"database": {"url": "sqlite:///a.db"}}},
        {"app": {"database": {"url": "sqlite:///b.db"}}},
        caplog,
    )
    assert any("database" in r.message for r in caplog.records)


def test_debug_change_warns(caplog):
    _check({"app": {"debug": False}}, {"app": {"debug": True}}, caplog)
    assert any("debug" in r.message for r in caplog.records)


def test_prometheus_enabled_change_warns(caplog):
    _check({"api": {"prometheus": {"enabled": False}}}, {"api": {"prometheus": {"enabled": True}}}, caplog)
    assert any("prometheus" in r.message for r in caplog.records)


def test_prometheus_multiproc_dir_change_warns(caplog):
    _check(
        {"api": {"prometheus": {"multiproc_dir": "/a"}}},
        {"api": {"prometheus": {"multiproc_dir": "/b"}}},
        caplog,
    )
    assert any("multiproc_dir" in r.message for r in caplog.records)


def test_oauth2_client_id_change_warns(caplog):
    _check({"oauth2": {"client_id": "aaa"}}, {"oauth2": {"client_id": "bbb"}}, caplog)
    assert any("oauth2" in r.message for r in caplog.records)


def test_oauth2_added_key_warns(caplog):
    _check({}, {"oauth2": {"client_secret": "xyz"}}, caplog)
    assert any("oauth2" in r.message for r in caplog.records)


def test_app_name_change_no_warning(caplog):
    """app.name is hot-reloadable — changing it must not emit a restart warning."""
    _check({"app": {"name": "Lumen"}}, {"app": {"name": "My Lumen"}}, caplog)
    assert caplog.records == []


def test_chat_config_change_no_warning(caplog):
    """chat.* keys are hot-reloadable — no restart warning."""
    _check({"chat": {"remove": "hide"}}, {"chat": {"remove": "delete"}}, caplog)
    assert caplog.records == []


def test_restart_keys_covered():
    """Smoke-check that the known restart-required keys are present in RESTART_REQUIRED."""
    from lumen.services.config_watcher import RESTART_REQUIRED
    keys = {tuple(p) for p in RESTART_REQUIRED}
    assert ("app", "secret_key") in keys
    assert ("app", "database") in keys
    assert ("api", "prometheus", "enabled") in keys
    # Both are read once in create_app and never re-read by apply_hot_config, so
    # without an entry here the editor accepts a change that silently does nothing.
    assert ("app", "encryption_key") in keys
    assert ("app", "logs", "level") in keys


def test_start_config_watcher_creates_daemon_thread(app, tmp_path):
    from unittest.mock import MagicMock, patch

    from lumen.services.config_watcher import start_config_watcher

    config_path = str(tmp_path / "config.yaml")
    with patch("lumen.services.config_watcher.threading.Thread") as mock_cls:
        mock_thread = MagicMock()
        mock_cls.return_value = mock_thread
        start_config_watcher(app, config_path)

    mock_cls.assert_called_once()
    assert mock_cls.call_args[1]["daemon"] is True
    mock_thread.start.assert_called_once()


def test_watcher_reloads_config_on_mtime_change(app, tmp_path, restore_config):
    from unittest.mock import patch

    import yaml

    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"version": 3, "app": {"name": "Reloaded"}}))

    sleep_count = 0

    def fake_sleep(n):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise SystemExit("stop")

    mtime_values = [1.0, 2.0]
    mtime_idx = 0

    def fake_getmtime(path):
        nonlocal mtime_idx
        v = mtime_values[mtime_idx] if mtime_idx < len(mtime_values) else 2.0
        mtime_idx += 1
        return v

    with patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep):
        with patch("lumen.services.config_watcher.os.path.getmtime", side_effect=fake_getmtime):
            try:
                _watcher(app, str(config_file))
            except SystemExit:
                pass

    with app.app_context():
        assert app.config.get("APP_NAME") == "Reloaded"


def test_watcher_skips_reload_of_old_config_version(app, tmp_path, restore_config, caplog):
    """A reload with version < 3 is skipped with an error; the running config is untouched."""
    from unittest.mock import patch

    import yaml

    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"version": 2, "app": {"name": "OldVersion"}}))
    with app.app_context():
        app.config["APP_NAME"] = "Original"

    sleep_count = 0

    def fake_sleep(n):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise SystemExit("stop")

    mtime_values = [1.0, 2.0]
    mtime_idx = 0

    def fake_getmtime(path):
        nonlocal mtime_idx
        v = mtime_values[mtime_idx] if mtime_idx < len(mtime_values) else 2.0
        mtime_idx += 1
        return v

    with caplog.at_level(logging.ERROR, logger="lumen.services.config_watcher"), \
         patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep), \
         patch("lumen.services.config_watcher.os.path.getmtime", side_effect=fake_getmtime):
        try:
            _watcher(app, str(config_file))
        except SystemExit:
            # fake_sleep raises SystemExit to break out of the watcher's infinite
            # loop after a fixed number of iterations; reaching here is the
            # expected end of the run, not a failure.
            pass

    with app.app_context():
        assert app.config.get("APP_NAME") == "Original"
    assert any("version: 3" in r.getMessage() for r in caplog.records)


def test_watcher_skips_when_mtime_unchanged(app, tmp_path, restore_config):
    from unittest.mock import patch

    import yaml

    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"app": {"name": "Original"}}))
    with app.app_context():
        app.config["APP_NAME"] = "Original"

    sleep_count = 0

    def fake_sleep(n):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise SystemExit("stop")

    with patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep):
        with patch("lumen.services.config_watcher.os.path.getmtime", return_value=1.0):
            try:
                _watcher(app, str(config_file))
            except SystemExit:
                pass

    with app.app_context():
        assert app.config.get("APP_NAME") == "Original"


def test_watcher_handles_read_error_gracefully(app, tmp_path, restore_config):
    from unittest.mock import patch

    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text("app:\n  name: Test\n")

    sleep_count = 0

    def fake_sleep(n):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise SystemExit("stop")

    mtime_values = [1.0, 2.0]
    mtime_idx = 0

    def fake_getmtime(path):
        nonlocal mtime_idx
        v = mtime_values[mtime_idx] if mtime_idx < len(mtime_values) else 2.0
        mtime_idx += 1
        return v

    with patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep):
        with patch("lumen.services.config_watcher.os.path.getmtime", side_effect=fake_getmtime):
            with patch("builtins.open", side_effect=OSError("disk error")):
                try:
                    _watcher(app, str(config_file))
                except SystemExit:
                    pass


def test_dev_user_set_logs_warning(app, caplog, restore_config):
    """A configured dev_user emits a loud warning so an accidental prod setting is visible."""
    import logging

    from lumen.services.config_watcher import apply_hot_config
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        with app.app_context():
            apply_hot_config(app, {"app": {"dev_user": "dev@example.com"}})
    assert any("DEV LOGIN ENABLED" in r.message for r in caplog.records)


def test_no_dev_user_no_warning(app, caplog, restore_config):
    import logging

    from lumen.services.config_watcher import apply_hot_config
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        with app.app_context():
            apply_hot_config(app, {"app": {}})
    assert not any("DEV LOGIN ENABLED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# apply_hot_config: global defaults and config-editor flag
# ---------------------------------------------------------------------------

def test_apply_hot_config_model_and_token_defaults(app, restore_config):
    from lumen.services.config_watcher import apply_hot_config
    yaml_data = {
        "version": 2,
        "defaults": {
            "models": {"ack_message": "please ack"},
            "tokens": {"max": 500, "refresh": 50, "starting": 250},
        },
    }
    with app.app_context():
        apply_hot_config(app, yaml_data)
        from lumen.services.config_watcher import DEFAULT_EARLY_ACCESS_MESSAGE
        assert app.config["MODEL_DEFAULTS"] == {"ack_message": "please ack",
                                                "early_access_message": DEFAULT_EARLY_ACCESS_MESSAGE}
        assert app.config["TOKEN_DEFAULTS"] == {"max": 500, "refresh": 50, "starting": 250}


def test_apply_hot_config_defaults_when_absent(app, restore_config):
    """With no defaults block, ack_message is unset and token pool defaults to zeros."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2})
        assert "access" not in app.config["MODEL_DEFAULTS"]
        assert app.config["MODEL_DEFAULTS"]["ack_message"] is None
        assert app.config["TOKEN_DEFAULTS"] == {"max": 0, "refresh": 0, "starting": 0}


def test_apply_hot_config_defaults_models_access_never_lands(app, restore_config):
    """The removed defaults.models.access key never lands in MODEL_DEFAULTS."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 3, "defaults": {"models": {"access": "allowed"}}})
    assert "access" not in app.config["MODEL_DEFAULTS"]


def test_apply_hot_config_token_starting_defaults_to_max(app, restore_config):
    """When 'starting' is omitted it falls back to 'max'."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "defaults": {"tokens": {"max": 300, "refresh": 30}}})
        assert app.config["TOKEN_DEFAULTS"] == {"max": 300, "refresh": 30, "starting": 300}


def test_apply_hot_config_legacy_graylist_notice_feeds_ack_message(app, restore_config):
    """Legacy app.graylist_default_notice maps to defaults.models.ack_message when unset."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "app": {"graylist_default_notice": "legacy notice"}})
        assert app.config["MODEL_DEFAULTS"]["ack_message"] == "legacy notice"


def test_apply_hot_config_config_editor_default_true(app, restore_config):
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "app": {}})
        assert app.config["CONFIG_EDITOR"] is True


def test_apply_hot_config_config_editor_disabled(app, restore_config):
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "app": {"config_editor": False}})
        assert app.config["CONFIG_EDITOR"] is False


def test_config_version_ok_gate():
    """Only integer version 3 configs pass; other versions are rejected."""
    from lumen.services.config_watcher import config_version_ok
    assert config_version_ok({"version": 3}) is True
    assert config_version_ok({"version": 4}) is False
    assert config_version_ok({"version": 2}) is False
    assert config_version_ok({"version": 1}) is False
    assert config_version_ok({"version": 3.0}) is False
    assert config_version_ok({"version": "3"}) is False
    assert config_version_ok({}) is False
    assert config_version_ok({"version": None}) is False
    assert config_version_ok({"version": "three"}) is False
    assert config_version_ok({"version": []}) is False


def test_create_app_refuses_old_config_version(tmp_path, monkeypatch):
    """create_app exits with a clear error when config.yaml is not version 3."""
    import pytest
    import yaml as _yaml
    cfg = tmp_path / "old.yaml"
    cfg.write_text(_yaml.dump({"version": 2, "app": {"secret_key": "x", "encryption_key": "y",
                                                     "database": {"url": "sqlite:///:memory:"}},
                               "models": [{"name": "m", "input_cost_per_million": 0,
                                           "output_cost_per_million": 0}]}))
    import config as config_module
    monkeypatch.setattr(config_module.Config, "CONFIG_YAML", str(cfg))
    from lumen import create_app
    with pytest.raises(SystemExit):
        create_app()


def test_create_app_refuses_non_numeric_config_version(tmp_path, monkeypatch, capsys):
    """Malformed version values use the normal startup error instead of traceback."""
    import pytest
    import yaml as _yaml

    cfg = tmp_path / "malformed-version.yaml"
    cfg.write_text(_yaml.dump({
        "version": "three",
        "app": {
            "secret_key": "x",
            "encryption_key": "y",
            "database": {"url": "sqlite:///:memory:"},
        },
        "models": [{
            "name": "m",
            "input_cost_per_million": 0,
            "output_cost_per_million": 0,
        }],
    }))
    import config as config_module

    monkeypatch.setattr(config_module.Config, "CONFIG_YAML", str(cfg))
    from lumen import create_app

    with pytest.raises(SystemExit):
        create_app()
    assert "config.yaml must declare 'version: 3'" in capsys.readouterr().err


@pytest.mark.parametrize("weak_key", ["secret_key", "encryption_key"])
def test_create_app_refuses_short_security_keys(tmp_path, monkeypatch, caplog, weak_key):
    """Session signing and credential encryption require 32-character keys."""
    import yaml as _yaml

    app_config = {
        "secret_key": "s" * 32,
        "encryption_key": "e" * 32,
        "database": {"url": "sqlite:///:memory:"},
    }
    app_config[weak_key] = "x" * 31
    cfg = tmp_path / "short-key.yaml"
    cfg.write_text(_yaml.dump({
        "version": 3,
        "app": app_config,
        "models": [{"model_name": "test-model"}],
    }))
    import config as config_module

    monkeypatch.setattr(config_module.Config, "CONFIG_YAML", str(cfg))
    monkeypatch.delenv("LUMEN_SECRET_KEY", raising=False)
    monkeypatch.delenv("LUMEN_ENCRYPTION_KEY", raising=False)
    from lumen import create_app

    with pytest.raises(SystemExit):
        create_app()
    assert weak_key in caplog.text
    assert "at least 32 characters" in caplog.text


def test_create_app_refuses_short_environment_key(tmp_path, monkeypatch, caplog):
    """A weak environment override cannot bypass startup validation."""
    import yaml as _yaml

    cfg = tmp_path / "strong-keys.yaml"
    cfg.write_text(_yaml.dump({
        "version": 3,
        "app": {
            "secret_key": "s" * 32,
            "encryption_key": "e" * 32,
            "database": {"url": "sqlite:///:memory:"},
        },
        "models": [{"model_name": "test-model"}],
    }))
    import config as config_module

    monkeypatch.setattr(config_module.Config, "CONFIG_YAML", str(cfg))
    monkeypatch.setenv("LUMEN_ENCRYPTION_KEY", "short")
    from lumen import create_app

    with pytest.raises(SystemExit):
        create_app()
    assert "LUMEN_ENCRYPTION_KEY" in caplog.text
    assert "at least 32 characters" in caplog.text


def test_unknown_app_key_warns(app, caplog, restore_config):
    """A key renamed by a schema change must not fail silently.

    Every read of the app section is a .get() with a default, so an orphaned key
    is simply ignored. `app.database_url` outlived the move to `app.database.url`
    by six weeks that way, which left the whole test suite running against the
    developer's dev database and dropping its tables on every run.
    """
    import logging

    from lumen.services.config_watcher import apply_hot_config
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        with app.app_context():
            apply_hot_config(app, {"app": {"database_url": "sqlite:///stale.db"}})
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "unrecognised key" in messages
    assert "database_url" in messages


def test_known_app_keys_do_not_warn(app, caplog, restore_config):
    import logging

    from lumen.services.config_watcher import KNOWN_APP_KEYS, apply_hot_config
    # Empty dicts are inert placeholders for every shape these keys can take
    # (str, bool, mapping); the point is only that the key name is recognised.
    yaml_data = {"app": {key: {} for key in KNOWN_APP_KEYS}}
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        with app.app_context():
            apply_hot_config(app, yaml_data)
    assert not any("unrecognised key" in r.getMessage() for r in caplog.records)


def test_every_app_cfg_read_is_in_known_app_keys():
    """Reverse guard: every key read off ``app_cfg`` must be recognised.

    KNOWN_APP_KEYS is the allowlist that turns a stale, silently-ignored key
    into a loud warning. Its forward test (test_known_app_keys_do_not_warn)
    builds its input from the constant itself, so it cannot catch the constant
    drifting out of step with the reads in apply_hot_config / _apply_theme —
    which is exactly what happened when the set shipped incomplete. This walks
    the module's AST instead and asserts the set actually covers every read.
    """
    import ast
    from pathlib import Path

    from lumen.services.config_watcher import KNOWN_APP_KEYS

    source = Path(
        Path(__file__).resolve().parents[2] / "lumen" / "services" / "config_watcher.py"
    ).read_text()
    tree = ast.parse(source)

    read_keys = set()
    for node in ast.walk(tree):
        # app_cfg.get("key", default) — first positional literal is the read
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app_cfg"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            read_keys.add(node.args[0].value)
        # app_cfg["key"] — subscription form
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "app_cfg"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            read_keys.add(node.slice.value)

    assert read_keys, "no app_cfg reads found — the AST walk may be stale"
    assert read_keys <= set(KNOWN_APP_KEYS), (
        f"app_cfg keys read but missing from KNOWN_APP_KEYS: {sorted(read_keys - set(KNOWN_APP_KEYS))}"
    )


def test_shipped_configs_have_no_unknown_app_keys():
    """The fixture and the shipped configs must stay in step with the schema.

    This is the check that would have caught the database_url regression on the
    day the key moved, rather than six weeks later.
    """
    from pathlib import Path

    import yaml as _yaml

    from lumen.services.config_watcher import KNOWN_APP_KEYS
    root = Path(__file__).resolve().parents[2]
    for rel in ("tests/fixtures/test_config.yaml", "config.yaml.example", "dev.config.yaml"):
        data = _yaml.safe_load((root / rel).read_text()) or {}
        unknown = sorted(set(data.get("app") or {}) - KNOWN_APP_KEYS)
        assert not unknown, f"{rel} has unrecognised app key(s): {unknown}"


def _chart_app_keys():
    """Keys the Helm chart writes under 'app:' in the config.yaml it generates.

    Parsed rather than rendered because helm is not a test dependency. Template
    action lines ({{- if ... }}) are skipped, so a key emitted only under a
    condition still counts — it can reach a deployed config.yaml.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "chart" / "templates" / "config-secret.yaml").read_text()
    keys, in_app = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{{"):
            continue
        if line == "    app:":
            in_app = True
            continue
        if in_app:
            if len(line) - len(line.lstrip()) <= 4:  # left the app: block
                break
            if m := re.match(r"^      ([A-Za-z_]+):", line):
                keys.append(m.group(1))
    return keys


def test_chart_generated_app_keys_do_not_warn(app, caplog, restore_config):
    """A Helm-deployed config.yaml must not warn about its own keys.

    `config_editor` is emitted unconditionally by the chart but was missing from
    KNOWN_APP_KEYS, so every Helm deploy logged that it was ignored — while it
    was in fact applied, and defaults to True (read-write admin config editor)
    if an operator deletes it to silence the warning.
    """
    import logging

    from lumen.services.config_watcher import KNOWN_APP_KEYS, apply_hot_config

    keys = _chart_app_keys()
    # Guard the parser itself: a template restructure must not silently empty this.
    assert "config_editor" in keys and "database" in keys, keys

    unknown = sorted(set(keys) - KNOWN_APP_KEYS)
    assert not unknown, f"chart/templates/config-secret.yaml emits unrecognised app key(s): {unknown}"

    # Empty dicts are inert placeholders for every shape these keys can take.
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        with app.app_context():
            apply_hot_config(app, {"app": dict.fromkeys(keys, {})})
    assert not any("unrecognised key" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# apply_hot_config: global defaults and config-editor flag (orthogonal access)
# ---------------------------------------------------------------------------


def test_apply_hot_config_llm_bounds_from_yaml(app, restore_config):
    from lumen.services.config_watcher import apply_hot_config
    yaml_data = {
        "version": 2,
        "llm": {"connect_timeout": 3, "read_timeout": 45, "max_retries": 0},
    }
    with app.app_context():
        apply_hot_config(app, yaml_data)
        assert app.config["LLM_CONNECT_TIMEOUT"] == 3.0
        assert app.config["LLM_READ_TIMEOUT"] == 45.0
        assert app.config["LLM_MAX_RETRIES"] == 0


def test_apply_hot_config_llm_defaults_when_section_absent(app, restore_config):
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2})
        assert app.config["LLM_CONNECT_TIMEOUT"] == 5.0
        assert app.config["LLM_READ_TIMEOUT"] == 300.0
        assert app.config["LLM_MAX_RETRIES"] == 1


def test_apply_hot_config_llm_defaults_for_missing_keys(app, restore_config):
    """A partial llm section keeps the defaults for the keys it omits."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "llm": {"read_timeout": 30}})
        assert app.config["LLM_CONNECT_TIMEOUT"] == 5.0
        assert app.config["LLM_READ_TIMEOUT"] == 30.0
        assert app.config["LLM_MAX_RETRIES"] == 1


def test_apply_hot_config_llm_blank_key_uses_default(app, restore_config):
    """'read_timeout:' with no value parses as None and must fall back, not crash."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "llm": {"read_timeout": None}})
        assert app.config["LLM_READ_TIMEOUT"] == 300.0


def test_apply_hot_config_llm_values_are_floats_and_int(app, restore_config):
    """Values are coerced so the SDK never receives a yaml string."""
    from lumen.services.config_watcher import apply_hot_config
    with app.app_context():
        apply_hot_config(app, {"version": 2, "llm": {"connect_timeout": "2.5", "max_retries": "3"}})
        assert app.config["LLM_CONNECT_TIMEOUT"] == 2.5
        assert isinstance(app.config["LLM_CONNECT_TIMEOUT"], float)
        assert app.config["LLM_MAX_RETRIES"] == 3
        assert isinstance(app.config["LLM_MAX_RETRIES"], int)


def test_watcher_reloads_llm_bounds(app, tmp_path, restore_config):
    """A changed llm section is picked up by a hot reload, not just at startup."""
    from unittest.mock import patch

    import yaml

    from lumen.services.config_watcher import _watcher, apply_hot_config

    with app.app_context():
        apply_hot_config(app, {"version": 2, "llm": {"read_timeout": 10, "max_retries": 0}})
        assert app.config["LLM_READ_TIMEOUT"] == 10.0

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"version": 3, "llm": {"read_timeout": 90, "max_retries": 2}}))

    sleep_count = 0

    def fake_sleep(n):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise SystemExit("stop")

    mtime_values = [1.0, 2.0]
    mtime_idx = 0

    def fake_getmtime(path):
        nonlocal mtime_idx
        v = mtime_values[mtime_idx] if mtime_idx < len(mtime_values) else 2.0
        mtime_idx += 1
        return v

    with patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep), \
         patch("lumen.services.config_watcher.sync_models_from_yaml"), \
         patch("lumen.services.config_watcher.os.path.getmtime", side_effect=fake_getmtime):
        try:
            _watcher(app, str(config_file))
        except SystemExit:
            pass

    with app.app_context():
        assert app.config["LLM_READ_TIMEOUT"] == 90.0
        assert app.config["LLM_MAX_RETRIES"] == 2


def test_shipped_config_example_has_llm_section(app, restore_config):
    """config.yaml.example documents the section and it loads to the stated defaults."""
    from pathlib import Path

    import yaml as _yaml

    from lumen.services.config_watcher import apply_hot_config
    root = Path(__file__).resolve().parents[2]
    data = _yaml.safe_load((root / "config.yaml.example").read_text())
    assert set(data["llm"]) == {"connect_timeout", "read_timeout", "request_timeout", "max_retries"}
    with app.app_context():
        apply_hot_config(app, data)
        assert app.config["LLM_CONNECT_TIMEOUT"] == 5.0
        assert app.config["LLM_READ_TIMEOUT"] == 300.0
        assert app.config["LLM_REQUEST_TIMEOUT"] == 600.0
        assert app.config["LLM_MAX_RETRIES"] == 1
# ---------------------------------------------------------------------------
# validate_config_structure
# ---------------------------------------------------------------------------

def _valid_config():
    return {
        "version": 3,
        "models": [{
            "name": "m",
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2,
            "endpoints": [{"url": "http://x/v1", "api_key": "k"}],
        }],
    }


def test_validate_config_accepts_valid_config():
    from lumen.services.config_watcher import validate_config_structure
    assert validate_config_structure(_valid_config()) == []


def test_validate_config_accepts_model_without_endpoints():
    from lumen.services.config_watcher import validate_config_structure
    cfg = _valid_config()
    del cfg["models"][0]["endpoints"]
    assert validate_config_structure(cfg) == []


def test_validate_config_rejects_missing_version():
    from lumen.services.config_watcher import validate_config_structure
    cfg = _valid_config()
    del cfg["version"]
    assert any("version" in e for e in validate_config_structure(cfg))


def test_validate_config_rejects_missing_model_name():
    from lumen.services.config_watcher import validate_config_structure
    cfg = _valid_config()
    del cfg["models"][0]["name"]
    assert any("name" in e for e in validate_config_structure(cfg))


def test_validate_config_rejects_non_numeric_costs():
    from lumen.services.config_watcher import validate_config_structure
    cfg = _valid_config()
    cfg["models"][0]["input_cost_per_million"] = "cheap"
    errors = validate_config_structure(cfg)
    assert any("input_cost_per_million" in e for e in errors)


def test_validate_config_rejects_missing_costs():
    from lumen.services.config_watcher import validate_config_structure
    cfg = _valid_config()
    del cfg["models"][0]["output_cost_per_million"]
    assert any("output_cost_per_million" in e for e in validate_config_structure(cfg))


def test_validate_config_rejects_endpoint_without_url_or_key():
    from lumen.services.config_watcher import validate_config_structure
    cfg = _valid_config()
    cfg["models"][0]["endpoints"] = [{"api_key": "k"}, {"url": "http://y/v1"}]
    errors = validate_config_structure(cfg)
    assert any("url" in e for e in errors)
    assert any("api_key" in e for e in errors)


def test_validate_config_rejects_non_mapping():
    from lumen.services.config_watcher import validate_config_structure
    assert validate_config_structure(["not", "a", "dict"]) == ["config must be a YAML mapping"]
