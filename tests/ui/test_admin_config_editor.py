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
        # Close the response: a static file stays open until it is.
        with resp:
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


MODELS_CONFIG = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: cached-model
    input_cost_per_million: 1
    output_cost_per_million: 2
    cached_input_cost_per_million: 0.1
    endpoints:
      - url: https://api.example.com/v1
        api_key: sk-real
"""


def test_editor_saves_cached_input_price(editor):
    """The Cached Input Cost box collects into the payload; blank removes the key."""
    page, cfg, posted = editor
    cfg.write_text(MODELS_CONFIG)

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-sel")
    page.wait_for_selector("#mdl-cached-in-cost")

    # Pre-filled from config; subtext explains the input-price fallback.
    assert page.input_value("#mdl-cached-in-cost") == "0.1"
    label = page.text_content("label[for='mdl-cached-in-cost']")
    assert "Cached Input Cost / 1M tokens ($)" in label
    assert "defaults to input price when unset" in label
    # An unset write rate is billed at 0, and its hint says only that.
    write_label = page.text_content("label[for='mdl-cached-write-cost']")
    assert "Cache Write Cost / 1M tokens ($)" in write_label
    assert "defaults to 0 when unset" in write_label
    assert "input price" not in write_label

    # Blank it out: the collector drops the key so the app falls back to input price.
    page.fill("#mdl-cached-in-cost", "")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")

    assert len(posted) == 1
    assert "cached_input_cost_per_million" not in posted[0]["models"][0]
    saved = yaml.safe_load(cfg.read_text())
    assert "cached_input_cost_per_million" not in saved["models"][0]

    # Set a value: it reaches both the payload and config.yaml.
    page.fill("#mdl-cached-in-cost", "0.05")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")

    assert len(posted) == 2
    assert posted[1]["models"][0]["cached_input_cost_per_million"] == 0.05
    saved = yaml.safe_load(cfg.read_text())
    assert saved["models"][0]["cached_input_cost_per_million"] == 0.05


EMBEDDING_CONFIG = """\
version: 3
app:
  name: Lumen
  secret_key: real-secret
models:
  - name: embed-model
    input_cost_per_million: 0.1
    output_cost_per_million: 0
    input_modalities: [text]
    output_modalities: {output}
    endpoints:
      - url: https://api.example.com/v1
        api_key: sk-real
"""


@pytest.mark.parametrize("output", ["[embedding]", "[]"])
def test_editor_keeps_the_embedding_marker(editor, output):
    """The embedding output checkbox is ticked for a marked model (and for the
    legacy empty list sync used to write) and saves as ["embedding"]."""
    page, cfg, posted = editor
    cfg.write_text(EMBEDDING_CONFIG.format(output=output))

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert page.is_checked("#mdl-out-embedding")
    assert not page.is_checked("#mdl-out-text")

    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")

    assert posted[0]["models"][0]["output_modalities"] == ["embedding"]
    assert yaml.safe_load(cfg.read_text())["models"][0]["output_modalities"] == ["embedding"]


def test_editor_marks_a_model_as_embedding(editor):
    """Ticking the embedding box by hand marks a model as an embedding model."""
    page, cfg, posted = editor
    cfg.write_text(MODELS_CONFIG)

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert not page.is_checked("#mdl-out-embedding")
    page.check("#mdl-out-embedding")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")

    assert yaml.safe_load(cfg.read_text())["models"][0]["output_modalities"] == ["embedding"]


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


@pytest.mark.parametrize("updates, toast, alert", [
    ({"context_window": 32768}, "Updated: context size. models.dev unreachable", "alert-success"),
    ({}, "models.dev unreachable", "alert-secondary"),
])
def test_editor_update_reports_unreachable_modelsdev(editor, monkeypatch, updates, toast, alert):
    """Update says "models.dev unreachable" when the fetch failed, both alongside
    the changes the endpoint supplied and when there are none."""
    from lumen.services import model_sync
    page, cfg, posted = editor
    cfg.write_text(MODELS_CONFIG)
    monkeypatch.setattr(model_sync, "sync_model", lambda model_def: {
        "updates": updates, "removals": [], "matched": None, "endpoint_ok": True,
        "modelsdev_price": None, "modelsdev_ok": False,
    })

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-ctx")
    page.click("#btn-sync-model")
    page.wait_for_selector(f"#cfg-toast .{alert}")

    assert page.text_content("#cfg-toast .alert").strip() == toast
    assert page.input_value("#mdl-ctx") == ("32768" if updates else "")
    assert posted[-1]["name"] == "cached-model"
