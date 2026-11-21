"""Run the Model Dashboard's stat-card filters in a real browser.

Uses the same hermetic page set-up as test_models_row_click.py: every
same-origin request goes to the Flask test client and Bootstrap's JS is served
from the pinned copy.
"""
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN
from tests.ui.test_models_dashboard import _add_model, _consent

ALL = ["acked-model", "bare-model", "degraded-model", "down-model", "ok-model"]


@pytest.fixture
def mixed_models(app, test_user):
    """One model per state; ``acked-model`` needs acknowledgment this user already gave."""
    _add_model(app, "ok-model", True)
    _add_model(app, "degraded-model", True, False)
    _add_model(app, "down-model", False, early_access=True)
    _add_model(app, "bare-model", needs_ack=True)
    acked = _add_model(app, "acked-model", True, needs_ack=True)
    _consent(app, test_user["id"], acked, consented_at=datetime(2026, 1, 1))


@pytest.fixture
def serve_models(auth_client, browser):
    """Return a function opening /models<query> in the browser -> (page, uncaught errors)."""
    def handle(route, request):
        url = urlsplit(request.url)
        if request.url == BOOTSTRAP_JS_URL:
            route.fulfill(status=200, content_type="application/javascript",
                          headers={"Access-Control-Allow-Origin": "*"}, body=BOOTSTRAP_JS.read_bytes())
            return
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()
            return
        with auth_client.get(url.path + (f"?{url.query}" if url.query else "")) as resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    pages = []

    def open_page(query=""):
        page = browser.new_page()
        pages.append(page)
        page.set_default_timeout(10_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(error))
        page.route("**/*", handle)
        page.goto(f"{ORIGIN}/models{query}")
        page.wait_for_selector("#model-list .model-row-toggle")
        return page, errors

    yield open_page
    for page in pages:
        page.close()


@pytest.fixture
def open_models(mixed_models, serve_models):
    return serve_models


def _card(key):
    return f'.model-stat[data-stat="{key}"]'


def _visible(page):
    return page.eval_on_selector_all("#model-list .model-row", "rows => rows.filter(r => r.offsetParent).map(r => r.dataset.model)")


def _pressed(page):
    return page.eval_on_selector_all('.model-stat[aria-pressed="true"]', "cards => cards.map(c => c.dataset.stat)")


def _empty_shown(page):
    return page.eval_on_selector("#model-list .model-filter-empty", "el => !!el.offsetParent")


@pytest.mark.parametrize(("key", "query", "rows"), [
    ("healthy", "?show=healthy", ["acked-model", "degraded-model", "ok-model"]),
    ("early-access", "?show=early_access", ["down-model"]),
    ("needs-ack", "?show=needs_ack", ["bare-model", "down-model"]),
])
def test_each_card_shows_exactly_its_rows(open_models, key, query, rows):
    page, errors = open_models()
    page.click(_card(key))
    assert sorted(_visible(page)) == rows
    count = int(page.text_content(f"{_card(key)} .model-stat-count"))
    assert count == len(rows)
    assert _pressed(page) == [key]
    assert urlsplit(page.url).query == query.lstrip("?")
    assert not _empty_shown(page)

    page.click(_card("models"))
    assert sorted(_visible(page)) == ALL
    assert _pressed(page) == ["models"]
    assert urlsplit(page.url).query == ""
    assert errors == []


def test_active_card_has_accent_border(open_models):
    page, _ = open_models("?show=healthy")
    border = page.eval_on_selector(_card("healthy"), "el => getComputedStyle(el).borderTopColor")
    other = page.eval_on_selector(_card("models"), "el => getComputedStyle(el).borderTopColor")
    assert border == "rgb(19, 41, 75)"
    assert other != border


def test_cards_work_from_the_keyboard(open_models):
    page, errors = open_models()
    page.focus(_card("early-access"))
    page.keyboard.press("Enter")
    assert _visible(page) == ["down-model"]
    page.focus(_card("healthy"))
    page.keyboard.press("Space")
    assert sorted(_visible(page)) == ["acked-model", "degraded-model", "ok-model"]
    assert _pressed(page) == ["healthy"]
    assert errors == []


def test_reload_keeps_the_view(open_models):
    page, errors = open_models()
    page.click(_card("needs-ack"))
    page.reload()
    page.wait_for_selector("#model-list .model-row-toggle", state="attached")
    assert _pressed(page) == ["needs-ack"]
    assert sorted(_visible(page)) == ["bare-model", "down-model"]
    assert errors == []


def test_open_row_that_leaves_the_view_is_closed(open_models):
    page, errors = open_models()
    page.click('li[data-model="bare-model"] .model-row-toggle')
    page.wait_for_function("!document.querySelector('#model-list .collapsing')")
    page.click(_card("healthy"))
    page.click(_card("models"))
    assert page.eval_on_selector_all("#model-list .collapse.show", "els => els.length") == 0
    assert page.get_attribute('li[data-model="bare-model"] .model-row-toggle', "aria-expanded") == "false"
    assert errors == []


def test_empty_view_shows_empty_line(test_model, serve_models):
    page, errors = serve_models()
    assert not _empty_shown(page)
    page.click(_card("needs-ack"))
    assert _visible(page) == []
    assert _empty_shown(page)
    assert page.text_content("#model-list .model-filter-empty").strip() == "No models in this view."
    page.click(_card("models"))
    assert _visible(page) == ["test-model"]
    assert not _empty_shown(page)
    assert errors == []
