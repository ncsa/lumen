"""Run /connect's OpenCode config generator in a real browser.

The config is assembled client-side from the page's embedded model data, so a
route test cannot see which cost fields reach the file. These tests load
/connect in headless Chromium, with every same-origin request answered by the
Flask test client (no live server), and read the generated JSON back.
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


def _models(page):
    page.goto(f"{ORIGIN}/connect")
    page.wait_for_selector("#examples-opencode pre code")
    config = json.loads(page.text_content("#examples-opencode pre code"))
    return next(iter(config["provider"].values()))["models"]


def _add_model(app, **fields):
    with app.app_context():
        db.session.add(ModelConfig(**fields))
        db.session.commit()


def test_config_omits_cache_prices_the_model_does_not_configure(connect_page, test_model):
    """A model without cache rates gets no cost.cache: the file states the
    instance's prices, and restating the input price as a cache price would claim
    a setting that does not exist."""
    entry = _models(connect_page)[test_model["model_name"]]
    assert entry["cost"] == {"input": 1.0, "output": 2.0}


def test_config_states_both_rates_when_the_model_configures_them(app, connect_page):
    _add_model(
        app,
        model_name="cached-model",
        input_cost_per_million=5.0,
        output_cost_per_million=15.0,
        cached_input_cost_per_million=0.5,
        cache_write_cost_per_million=5.0,
    )
    entry = _models(connect_page)["cached-model"]
    assert entry["cost"] == {
        "input": 5.0,
        "output": 15.0,
        "cache": {"read": 0.5, "write": 5.0},
    }


def test_config_omits_only_the_side_the_model_leaves_unset(app, connect_page):
    _add_model(
        app,
        model_name="read-only-cache",
        input_cost_per_million=2.0,
        output_cost_per_million=4.0,
        cached_input_cost_per_million=0.2,
    )
    entry = _models(connect_page)["read-only-cache"]
    assert entry["cost"]["cache"] == {"read": 0.2}
