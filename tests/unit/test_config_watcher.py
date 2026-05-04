"""Tests for config_watcher._check_restart_required, _watcher, and start_config_watcher."""
import logging


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


def test_database_url_change_warns(caplog):
    _check(
        {"app": {"database_url": "sqlite:///a.db"}},
        {"app": {"database_url": "sqlite:///b.db"}},
        caplog,
    )
    assert any("database_url" in r.message for r in caplog.records)


def test_debug_change_warns(caplog):
    _check({"app": {"debug": False}}, {"app": {"debug": True}}, caplog)
    assert any("debug" in r.message for r in caplog.records)


def test_prometheus_enabled_change_warns(caplog):
    _check({"prometheus": {"enabled": False}}, {"prometheus": {"enabled": True}}, caplog)
    assert any("prometheus" in r.message for r in caplog.records)


def test_prometheus_multiproc_dir_change_warns(caplog):
    _check({"prometheus": {"multiproc_dir": "/a"}}, {"prometheus": {"multiproc_dir": "/b"}}, caplog)
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
    """Smoke-check that the known restart-required keys are present in _RESTART_REQUIRED."""
    from lumen.services.config_watcher import _RESTART_REQUIRED
    keys = {(s, k) for s, k in _RESTART_REQUIRED}
    assert ("app", "secret_key") in keys
    assert ("app", "database_url") in keys
    assert ("prometheus", "enabled") in keys


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


def test_watcher_reloads_config_on_mtime_change(app, tmp_path):
    import yaml
    from unittest.mock import patch
    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"app": {"name": "Reloaded"}}))

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


def test_watcher_skips_when_mtime_unchanged(app, tmp_path):
    import yaml
    from unittest.mock import patch
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


def test_watcher_handles_read_error_gracefully(app, tmp_path):
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
