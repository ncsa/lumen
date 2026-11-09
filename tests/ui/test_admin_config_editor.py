"""Run the admin config editor's JavaScript in a real browser.

The editor renders and collects its forms client-side, so route tests that post
JSON directly cannot catch a collector that drops a setting. These tests load
/admin/config in headless Chromium, with every same-origin request answered by
the Flask test client (no live server), and drive the real save path.

The browser comes from the `browser` fixture in tests/ui/conftest.py.
"""
from urllib.parse import urlsplit

import pytest
import yaml

from tests.ui.test_accessibility import _run_all_checks

ORIGIN = "http://lumen.test"

_CONFIG = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
api:
  prometheus:
    enabled: true
    token: real-prom-token
  monitoring:
    token: real-mon-token
"""


@pytest.fixture
def editor(app, admin_client, browser, tmp_path):
    """Open /admin/config on a tmp config.yaml; yield (page, config path, posted payloads)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG)
    original = app.config["CONFIG_YAML"]
    app.config["CONFIG_YAML"] = str(cfg)
    posted = []

    def handle(route, request):
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()  # CDN assets: keep the test hermetic
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        if request.method == "POST":
            posted.append(request.post_data_json)
            resp = admin_client.post(path, json=request.post_data_json)
        else:
            resp = admin_client.get(path)
        route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    page = browser.new_page()
    page.set_default_timeout(10_000)
    page.route("**/*", handle)
    try:
        yield page, cfg, posted
    finally:
        page.close()
        app.config["CONFIG_YAML"] = original


def _open_api_section(page):
    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-api")
    page.wait_for_selector("#keys-ttl")


def test_editor_saves_key_ttl_with_other_api_settings(editor):
    page, cfg, posted = editor
    _open_api_section(page)
    _run_all_checks(page.content().encode(), "/admin/config (API section)")

    page.fill("#keys-ttl", "30d")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")

    assert len(posted) == 1
    assert posted[0]["api"]["keys"] == {"ttl": "30d"}
    saved = yaml.safe_load(cfg.read_text())["api"]
    assert saved == {
        "keys": {"ttl": "30d"},
        "prometheus": {"enabled": True, "token": "real-prom-token"},
        "monitoring": {"token": "real-mon-token"},
    }


def test_editor_refuses_to_save_malformed_key_ttl(editor):
    page, cfg, posted = editor
    _open_api_section(page)

    page.fill("#keys-ttl", "abc")
    assert page.get_attribute("#keys-ttl", "aria-invalid") == "true"
    assert "12h, 30d or 8w" in page.text_content("#keys-ttl-err")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=API Keys")

    assert posted == []
    assert cfg.read_text() == _CONFIG


def test_editor_key_ttl_read_only_when_editor_disabled(app, editor):
    page, cfg, posted = editor
    original = app.config.get("CONFIG_EDITOR", True)
    app.config["CONFIG_EDITOR"] = False
    try:
        _open_api_section(page)
        assert page.get_attribute("#keys-ttl", "readonly") is not None
        assert not page.is_editable("#keys-ttl")
        assert page.is_disabled("#btn-save")
    finally:
        app.config["CONFIG_EDITOR"] = original
