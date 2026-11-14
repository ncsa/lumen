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
from lumen.models.model_alias import ModelAlias
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


# ── GitHub Copilot CLI (providers.json) ──────────────────────────────────────


def _copilot(page):
    """Return the generated providers.json, parsed."""
    page.goto(f"{ORIGIN}/connect")
    page.wait_for_selector("#examples-copilot pre code")
    return json.loads(page.text_content("#examples-copilot pre code"))


def _copilot_models(app, page):
    """Add one model per mapping case (plus an alias and an audio-only model)
    and return the generated entries by id."""
    common = {"input_cost_per_million": 1.0, "output_cost_per_million": 2.0}
    with app.app_context():
        reasoning = ModelConfig(
            model_name="cp-reasoning", context_window=524282, max_output_tokens=131072,
            input_modalities=["text"], supports_function_calling=True,
            supports_reasoning=True, **common,
        )
        db.session.add_all([
            reasoning,
            ModelConfig(
                model_name="cp-vision", context_window=262138, max_output_tokens=65536,
                input_modalities=["text", "image"], supports_function_calling=True,
                supports_reasoning=False, **common,
            ),
            ModelConfig(
                model_name="cp-no-tools", context_window=131066, max_output_tokens=32768,
                input_modalities=["text"], supports_function_calling=False, **common,
            ),
            ModelConfig(
                model_name="cp-no-output", context_window=131072,
                input_modalities=["text"], supports_function_calling=True, **common,
            ),
            ModelConfig(
                model_name="cp-audio-only", context_window=8192, max_output_tokens=1024,
                input_modalities=["audio"], **common,
            ),
        ])
        db.session.flush()
        db.session.add(ModelAlias(alias="cp-reasoning-old", model_config_id=reasoning.id))
        db.session.commit()
    config = _copilot(page)
    return config, {m["id"]: m for m in config["models"]}


def test_copilot_provider_block(connect_page):
    config = _copilot(connect_page)
    assert config["providers"] == [{
        "name": "lumen",
        "type": "openai",
        # The page is served through the Flask test client, whose root is localhost.
        "baseUrl": "http://localhost/v1",
        "wireApi": "completions",
        "apiKey": "YOUR_LUMEN_API_KEY",
    }]


def test_copilot_reasoning_model_gets_default_efforts(app, connect_page):
    _, models = _copilot_models(app, connect_page)
    assert models["cp-reasoning"] == {
        "id": "cp-reasoning",
        "provider": "lumen",
        "name": "cp-reasoning via Lumen",
        "maxPromptTokens": 524282 - 131072,
        "maxContextWindowTokens": 524282,
        "maxOutputTokens": 131072,
        "capabilities": {"supports": {
            "vision": False,
            "toolCalls": True,
            "thinking": True,
            "reasoning_effort": ["low", "medium", "high"],
        }},
    }


def test_copilot_vision_model_without_reasoning(app, connect_page):
    _, models = _copilot_models(app, connect_page)
    supports = models["cp-vision"]["capabilities"]["supports"]
    assert supports == {"vision": True, "toolCalls": True}
    assert "thinking" not in supports and "reasoning_effort" not in supports


def test_copilot_model_without_tools(app, connect_page):
    _, models = _copilot_models(app, connect_page)
    assert models["cp-no-tools"]["capabilities"]["supports"] == {"vision": False, "toolCalls": False}


def test_copilot_model_without_max_output_tokens(app, connect_page):
    _, models = _copilot_models(app, connect_page)
    entry = models["cp-no-output"]
    assert entry["maxContextWindowTokens"] == 131072
    assert entry["maxPromptTokens"] == 131072
    assert "maxOutputTokens" not in entry


def test_copilot_lists_canonical_text_models_only(app, connect_page, test_model):
    _, models = _copilot_models(app, connect_page)
    assert "cp-audio-only" not in models
    assert "cp-reasoning-old" not in models
    with app.app_context():
        canonical = set(db.session.execute(db.select(ModelConfig.model_name)).scalars())
    assert set(models) <= canonical
    assert set(models) == {"cp-reasoning", "cp-vision", "cp-no-tools", "cp-no-output",
                           test_model["model_name"]}
    assert all("alias of" not in m["name"] for m in models.values())
    assert all(m["provider"] == "lumen" for m in models.values())


def test_copilot_tab_names_the_file_and_the_script(connect_page):
    connect_page.goto(f"{ORIGIN}/connect")
    connect_page.wait_for_selector("#examples-copilot pre code")
    assert connect_page.text_content("#tab-copilot") == "Copilot CLI"
    assert connect_page.text_content("#examples-copilot .fw-semibold") == "providers.json"
    assert connect_page.text_content("#examples-copilot button.btn-primary") == "Download providers.json"
    pane = connect_page.text_content("#pane-copilot")
    assert "~/.copilot/providers.json" in pane
    assert "%USERPROFILE%\\.copilot\\providers.json" in pane
    assert "lumen.sh --copilot" in pane
