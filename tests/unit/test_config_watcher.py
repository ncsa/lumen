"""Tests for config_watcher._check_restart_required."""
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
