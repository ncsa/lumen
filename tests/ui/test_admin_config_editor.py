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


KEY_TTL_FIELDS = {
    "max_ttl": "#keys-max-ttl",
    "max_admin_ttl": "#keys-max-admin-ttl",
    "max_monitor_ttl": "#keys-max-monitor-ttl",
    "default_ttl": "#keys-default-ttl",
}


def _open_api_section(page):
    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-api")
    page.wait_for_selector("#keys-max-ttl")


def _write_keys(cfg, keys):
    data = yaml.safe_load(cfg.read_text())
    data["api"]["keys"] = keys
    cfg.write_text(yaml.safe_dump(data))


def _save(page):
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")


def test_editor_saves_key_ttls_with_other_api_settings(editor):
    page, cfg, posted = editor
    _open_api_section(page)
    _run_all_checks(page.content().encode(), "/admin/config (API section)")

    values = {"max_ttl": "90d", "max_admin_ttl": "1d", "max_monitor_ttl": "0", "default_ttl": "30d"}
    for key, sel in KEY_TTL_FIELDS.items():
        page.fill(sel, values[key])
    _save(page)

    assert len(posted) == 1
    assert posted[0]["api"]["keys"] == values
    saved = yaml.safe_load(cfg.read_text())["api"]
    assert saved == {
        "keys": values,
        "prometheus": {"enabled": True, "token": "real-prom-token"},
        "monitoring": {"token": "real-mon-token"},
    }


def test_editor_key_ttls_round_trip(editor):
    """Loaded values fill their fields and are saved back unchanged."""
    page, cfg, posted = editor
    keys = {"max_ttl": "8w", "max_admin_ttl": "12h", "max_monitor_ttl": "30d", "default_ttl": "0"}
    _write_keys(cfg, keys)
    _open_api_section(page)

    for key, sel in KEY_TTL_FIELDS.items():
        assert page.input_value(sel) == keys[key]
    _save(page)

    assert posted[0]["api"]["keys"] == keys
    assert yaml.safe_load(cfg.read_text())["api"]["keys"] == keys


def test_editor_key_ttl_placeholders(editor):
    page, cfg, posted = editor
    _open_api_section(page)
    assert page.get_attribute("#keys-max-admin-ttl", "placeholder") == "7d"
    assert page.get_attribute("#keys-max-monitor-ttl", "placeholder") == "0"


@pytest.mark.parametrize("edit", [None, "12h"])
def test_editor_migrates_legacy_ttl_to_max_ttl(editor, edit):
    page, cfg, posted = editor
    _write_keys(cfg, {"ttl": "30d", "default_ttl": "7d"})
    _open_api_section(page)

    assert page.input_value("#keys-max-ttl") == "30d"
    if edit is not None:
        page.fill("#keys-max-ttl", edit)
    _save(page)

    expected = {"max_ttl": edit or "30d", "default_ttl": "7d"}
    assert posted[0]["api"]["keys"] == expected
    assert yaml.safe_load(cfg.read_text())["api"]["keys"] == expected


def test_editor_max_ttl_wins_over_legacy_ttl(editor):
    page, cfg, posted = editor
    _write_keys(cfg, {"ttl": "8w", "max_ttl": "30d"})
    _open_api_section(page)

    assert page.input_value("#keys-max-ttl") == "30d"
    _save(page)

    assert posted[0]["api"]["keys"] == {"max_ttl": "30d"}


def test_editor_untouched_save_keeps_null_max_ttl_uncapped(editor):
    """A present-but-null max_ttl makes the server ignore ttl (no cap); saving must not turn ttl into a cap."""
    from lumen.services.config_watcher import _key_max_ttl

    page, cfg, posted = editor
    keys = {"max_ttl": None, "ttl": "30d", "default_ttl": "7d"}
    assert _key_max_ttl(keys) == 0
    _write_keys(cfg, keys)
    _open_api_section(page)

    assert page.input_value("#keys-max-ttl") == ""
    _save(page)

    expected = {"default_ttl": "7d"}
    assert posted[0]["api"]["keys"] == expected
    saved = yaml.safe_load(cfg.read_text())["api"]["keys"]
    assert saved == expected
    assert _key_max_ttl(saved) == 0


def test_editor_does_not_write_blank_key_ttls(editor):
    page, cfg, posted = editor
    _write_keys(cfg, {"max_ttl": "30d", "max_admin_ttl": "1d", "default_ttl": "7d", "future_key": "kept"})
    _open_api_section(page)

    page.fill("#keys-max-admin-ttl", "")
    page.fill("#keys-default-ttl", "  ")
    _save(page)

    expected = {"max_ttl": "30d", "future_key": "kept"}
    assert posted[0]["api"]["keys"] == expected
    assert yaml.safe_load(cfg.read_text())["api"]["keys"] == expected


def test_editor_drops_api_keys_when_every_field_is_blank(editor):
    page, cfg, posted = editor
    _write_keys(cfg, {"ttl": "30d"})
    _open_api_section(page)

    page.fill("#keys-max-ttl", "")
    _save(page)

    assert "keys" not in posted[0]["api"]
    assert "keys" not in yaml.safe_load(cfg.read_text())["api"]


@pytest.mark.parametrize("key", list(KEY_TTL_FIELDS))
def test_editor_refuses_to_save_malformed_key_ttl(editor, key):
    page, cfg, posted = editor
    _open_api_section(page)
    sel = KEY_TTL_FIELDS[key]

    page.fill(sel, "abc")
    assert page.get_attribute(sel, "aria-invalid") == "true"
    assert "12h, 30d or 8w" in page.text_content(f"{sel}-err")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=API Keys")

    assert posted == []
    assert cfg.read_text() == _CONFIG

    page.fill(sel, "30d")
    assert page.get_attribute(sel, "aria-invalid") == "false"
    assert page.text_content(f"{sel}-err") == ""


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
    input_modalities: {input}
    output_modalities: {output}
    endpoints:
      - url: https://api.example.com/v1
        api_key: sk-real
"""


def test_editor_keeps_the_embedding_marker(editor):
    """A marked model shows the embedding box ticked and saves as ["embedding"]."""
    page, cfg, posted = editor
    cfg.write_text(EMBEDDING_CONFIG.format(input="[text]", output="[embedding]"))

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert page.is_checked("#mdl-out-embedding")
    assert not page.is_checked("#mdl-out-text")

    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")

    assert posted[0]["models"][0]["output_modalities"] == ["embedding"]
    assert yaml.safe_load(cfg.read_text())["models"][0]["output_modalities"] == ["embedding"]


@pytest.mark.parametrize("inp", ["[text]", "[text, image]"])
def test_editor_leaves_an_empty_output_unknown(editor, inp):
    """An empty output list is ambiguous (models.dev sync writes it for chat
    models too), so the editor does not tick embedding for it, text-only included."""
    page, cfg, posted = editor
    cfg.write_text(EMBEDDING_CONFIG.format(input=inp, output="[]"))

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert not page.is_checked("#mdl-out-embedding")
    assert not page.is_checked("#mdl-out-text")


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


def test_editor_locks_output_cost_for_embedding(editor):
    """Ticking embedding sets Output Cost to 0 and makes it read-only, with a hint;
    unticking makes it editable again; saving writes an output price of 0."""
    page, cfg, posted = editor
    cfg.write_text(MODELS_CONFIG)

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert page.input_value("#mdl-out-cost") == "2"
    assert page.is_editable("#mdl-out-cost")
    assert not page.is_visible("#mdl-out-cost-hint")

    page.check("#mdl-out-embedding")
    assert page.input_value("#mdl-out-cost") == "0"
    assert not page.is_editable("#mdl-out-cost")
    assert page.is_visible("#mdl-out-cost-hint")
    assert page.text_content("#mdl-out-cost-hint").strip() == "embedding models have no output tokens"
    assert page.get_attribute("#mdl-out-cost", "aria-describedby") == "mdl-out-cost-hint"

    page.uncheck("#mdl-out-embedding")
    assert page.is_editable("#mdl-out-cost")
    assert not page.is_visible("#mdl-out-cost-hint")
    assert page.get_attribute("#mdl-out-cost", "aria-describedby") is None

    page.check("#mdl-out-embedding")
    page.click("#btn-save")
    page.wait_for_selector("#cfg-toast >> text=Saved successfully")
    assert posted[0]["models"][0]["output_cost_per_million"] == 0
    assert yaml.safe_load(cfg.read_text())["models"][0]["output_cost_per_million"] == 0


def test_editor_locks_output_cost_of_a_loaded_embedding_model(editor):
    """A model already marked as embedding renders with Output Cost locked at 0."""
    page, cfg, posted = editor
    cfg.write_text(EMBEDDING_CONFIG.format(input="[text]", output="[embedding]").replace(
        "output_cost_per_million: 0", "output_cost_per_million: 2"))

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert page.input_value("#mdl-out-cost") == "0"
    assert not page.is_editable("#mdl-out-cost")
    assert page.is_visible("#mdl-out-cost-hint")


def test_editor_output_cost_stays_read_only_with_automatic_price(editor):
    """With Automatic price on, unticking embedding leaves Output Cost read-only,
    and unticking Automatic price while embedding is ticked keeps it locked too."""
    page, cfg, posted = editor
    cfg.write_text(MODELS_CONFIG.replace("    output_cost_per_million: 2\n",
                                         "    output_cost_per_million: 2\n    auto_price: true\n"))

    page.goto(f"{ORIGIN}/admin/config")
    page.click("#nav-models")
    page.wait_for_selector("#mdl-out-embedding")
    assert page.is_checked("#mdl-autoprice")
    assert not page.is_editable("#mdl-out-cost")

    page.check("#mdl-out-embedding")
    assert not page.is_editable("#mdl-out-cost")
    page.uncheck("#mdl-autoprice")
    assert not page.is_editable("#mdl-out-cost")
    assert page.is_editable("#mdl-in-cost")
    page.check("#mdl-autoprice")
    page.uncheck("#mdl-out-embedding")
    assert not page.is_editable("#mdl-out-cost")
    page.uncheck("#mdl-autoprice")
    assert page.is_editable("#mdl-out-cost")


def test_editor_key_ttls_read_only_when_editor_disabled(app, editor):
    page, cfg, posted = editor
    original = app.config.get("CONFIG_EDITOR", True)
    app.config["CONFIG_EDITOR"] = False
    try:
        _open_api_section(page)
        for sel in KEY_TTL_FIELDS.values():
            assert page.get_attribute(sel, "readonly") is not None
            assert not page.is_editable(sel)
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
