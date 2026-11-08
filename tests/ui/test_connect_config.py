"""Run /connect's OpenCode config generator in a real browser.

The config is assembled client-side from the page's embedded model data, so a
route test cannot see which cost fields reach the file. These tests load
/connect in headless Chromium, with every same-origin request answered by the
Flask test client (no live server), and read the generated JSONC back.
"""
import json
from urllib.parse import urlsplit

import pytest

from lumen.extensions import db
from lumen.models.model_config import ModelConfig

ORIGIN = "http://lumen.test"


@pytest.fixture
def connect_page(auth_client, browser):
    def handle(route, request):
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()  # CDN assets: keep the test hermetic
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        resp = auth_client.get(path)
        route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    page = browser.new_page()
    page.set_default_timeout(10_000)
    page.route("**/*", handle)
    try:
        yield page
    finally:
        page.close()


def _without_comments(text):
    """Drop the // lines that explain prices the generator filled in."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _generated(page):
    """Return (models, raw text) of the generated opencode.jsonc."""
    page.goto(f"{ORIGIN}/connect")
    page.wait_for_selector("#examples-opencode pre code")
    text = page.text_content("#examples-opencode pre code")
    config = json.loads(_without_comments(text))
    return next(iter(config["provider"].values()))["models"], text


def _cache_block(text, model_id):
    """One model's cache block, from "cache": { to its closing brace."""
    after_model = text.split(f'"{model_id}": {{', 1)[1]
    return after_model.split('"cache": {', 1)[1].split("}", 1)[0]


def _add_model(app, **fields):
    with app.app_context():
        db.session.add(ModelConfig(**fields))
        db.session.commit()


def test_config_repeats_the_input_price_for_unset_cache_rates(connect_page, test_model):
    """OpenCode bills an omitted cache price as free, so both sides are stated:
    a model without cache rates repeats its input price, and a comment says the
    rates were filled in — Lumen charges exactly that."""
    models, text = _generated(connect_page)
    entry = models[test_model["model_name"]]
    assert entry["cost"] == {
        "input": 1.0,
        "output": 2.0,
        "cache": {"read": 1.0, "write": 1.0},
    }
    block = _cache_block(text, test_model["model_name"])
    assert "// No cache rates are configured, so both repeat the input price:" in block
    assert "OpenCode charges" in block
    assert "nothing for a cache price the config does not state." in block


def test_config_states_configured_rates_and_adds_no_comment(app, connect_page):
    _add_model(
        app,
        model_name="cached-model",
        input_cost_per_million=5.0,
        output_cost_per_million=15.0,
        cached_input_cost_per_million=0.5,
        cache_write_cost_per_million=5.0,
    )
    models, text = _generated(connect_page)
    assert models["cached-model"]["cost"] == {
        "input": 5.0,
        "output": 15.0,
        "cache": {"read": 0.5, "write": 5.0},
    }
    assert "//" not in _cache_block(text, "cached-model")


def test_config_fills_only_the_side_the_model_leaves_unset(app, connect_page):
    _add_model(
        app,
        model_name="read-only-cache",
        input_cost_per_million=2.0,
        output_cost_per_million=4.0,
        cached_input_cost_per_million=0.2,
    )
    models, text = _generated(connect_page)
    assert models["read-only-cache"]["cost"]["cache"] == {"read": 0.2, "write": 2.0}
    assert "// No cache-write rate is configured, so it repeats the input price:" in _cache_block(
        text, "read-only-cache"
    )


def test_generated_file_is_named_jsonc(connect_page):
    """The file carries comments, so it is offered as opencode.jsonc — OpenCode
    reads either extension."""
    connect_page.goto(f"{ORIGIN}/connect")
    connect_page.wait_for_selector("#examples-opencode pre code")
    assert connect_page.text_content("#examples-opencode .fw-semibold") == "opencode.jsonc"
    assert connect_page.text_content("#examples-opencode button.btn-primary") == "Download config.jsonc"
    assert "OpenCode reads opencode.jsonc as well as opencode.json" in connect_page.text_content(
        "#examples-opencode"
    )
