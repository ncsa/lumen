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

# Bootstrap's CSS, served from a pinned copy so the subtitle colors resolve its variables as in production.
BOOTSTRAP_CSS_URL = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css"
BOOTSTRAP_CSS = BOOTSTRAP_JS.parent / "bootstrap-5.3.8.min.css"

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
        if request.url == BOOTSTRAP_CSS_URL:
            route.fulfill(status=200, content_type="text/css",
                          headers={"Access-Control-Allow-Origin": "*"}, body=BOOTSTRAP_CSS.read_bytes())
            return
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
        page.wait_for_selector("#model-list li", state="attached")
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


def _theme_color(page, name):
    """The page's computed theme variable, resolved to the rgb() form getComputedStyle reports."""
    return page.evaluate("""name => {
        const probe = document.createElement('span');
        probe.style.color = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        document.body.appendChild(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
    }""", name)


def _shadow(page, key):
    return page.eval_on_selector(_card(key), "el => getComputedStyle(el).boxShadow")


def test_active_card_has_theme_border_only(open_models):
    page, _ = open_models()
    border = page.eval_on_selector(_card("models"), "el => getComputedStyle(el).borderTopColor")
    other = page.eval_on_selector(_card("early-access"), "el => getComputedStyle(el).borderTopColor")
    assert border == _theme_color(page, "--theme-primary")
    assert other != border
    assert _shadow(page, "models") == "none"


def test_healthy_card_has_no_bar_while_a_model_is_down(open_models):
    """The down state colors the subtitle only; the card itself gets no bar, active or not."""
    page, _ = open_models("?show=healthy")
    assert page.eval_on_selector(_card("healthy"), "el => el.classList.contains('model-stat-health-down')")
    assert _shadow(page, "healthy") == "none"


def test_bootstrap_css_copy_matches_base_template():
    base = (BOOTSTRAP_JS.parents[3] / "lumen" / "templates" / "base.html").read_text()
    assert f'href="{BOOTSTRAP_CSS_URL}"' in base, "update BOOTSTRAP_CSS_URL and the copy in tests/fixtures/vendor"


def _subtitle_color(page, key):
    return page.eval_on_selector(f"{_card(key)} .model-stat-subtitle", "el => getComputedStyle(el).color")


@pytest.mark.parametrize(("health", "color"), [
    ([(False,), (True, False)], "rgb(220, 53, 69)"),    # down -> --bs-danger
    ([(True, False), ()], "rgb(102, 77, 3)"),           # degraded only -> --bs-warning-text-emphasis
    ([(True,), ()], None),                               # no-endpoint only -> muted like the other cards
])
def test_healthy_subtitle_color(app, serve_models, health, color):
    for i, endpoints in enumerate(health):
        _add_model(app, f"model-{i}", *endpoints)
    page, errors = serve_models()
    # The colors come from Bootstrap's variables, so make sure its stylesheet applied.
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--bs-danger').trim()") == "#dc3545"
    muted = _subtitle_color(page, "early-access")
    assert _subtitle_color(page, "healthy") == (color or muted)
    assert muted not in ("rgb(220, 53, 69)", "rgb(102, 77, 3)")
    assert errors == []


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


def test_empty_models_view_then_filter(serve_models):
    page, errors = serve_models()

    def lines():
        return page.eval_on_selector_all("#model-list li", "lis => lis.filter(l => l.offsetParent).map(l => l.textContent.trim())")

    assert lines() == ["No models configured."]
    page.click(_card("healthy"))
    assert lines() == ["No models in this view."]
    page.click(_card("models"))
    assert lines() == ["No models configured."]
    assert errors == []


@pytest.mark.parametrize("back", [True, False])
def test_row_filtered_out_while_opening_ends_closed(open_models, back):
    """Filter a row out while its panel is still opening, then (optionally) switch straight back."""
    page, errors = open_models()
    # The tests do not load Bootstrap's CSS; add its collapse transition so the
    # panel really animates, as in production.
    page.add_style_tag(content=".collapse:not(.show){display:none}.collapsing{height:0;overflow:hidden;transition:height .35s ease}")
    # One task, so the clicks land while Bootstrap's opening transition is still running.
    page.evaluate("""back => {
        document.querySelector('li[data-model="bare-model"] .model-row-toggle').click();
        if (!document.querySelector('li[data-model="bare-model"] .collapsing')) throw new Error('not opening');
        document.querySelector('.model-stat[data-stat="healthy"]').click();
        if (back) document.querySelector('.model-stat[data-stat="models"]').click();
    }""", back)
    page.wait_for_timeout(600)  # past the transition and Bootstrap's shown event
    if not back:
        page.click(_card("models"))
    assert page.eval_on_selector_all("#model-list .collapse.show", "els => els.length") == 0
    assert page.get_attribute('li[data-model="bare-model"] .model-row-toggle', "aria-expanded") == "false"
    # The row still opens normally afterwards.
    page.click('li[data-model="bare-model"] .model-row-toggle')
    page.wait_for_function("!document.querySelector('#model-list .collapsing')")
    assert page.eval_on_selector_all("#model-list .collapse.show", "els => els.map(e => e.closest('li').dataset.model)") == ["bare-model"]
    assert errors == []


# ── Sortable columns ─────────────────────────────────────────────────────────

@pytest.fixture
def priced_models(app):
    """Early-access ``dear`` and ``cheap`` around a regular ``mid`` model; ``cheap`` has no context window."""
    _add_model(app, "a-dear", True, early_access=True, input_cost_per_million=9, output_cost_per_million=9, context_window=1_000_000)
    _add_model(app, "b-mid", True, input_cost_per_million=2, output_cost_per_million=5, context_window=8_000)
    _add_model(app, "c-cheap", True, early_access=True, input_cost_per_million=1, output_cost_per_million=1)


def _order(page):
    return page.eval_on_selector_all("#model-list .model-row", "rows => rows.map(r => r.dataset.model)")


def _sort(key):
    return f'.model-sort-btn[data-sort="{key}"]'


def _query(page):
    return dict(p.split("=") for p in urlsplit(page.url).query.split("&") if p)


def test_price_click_puts_cheapest_first_and_second_click_reverses(priced_models, serve_models):
    page, errors = serve_models()
    assert _order(page) == ["a-dear", "b-mid", "c-cheap"]
    page.click(_sort("price"))
    assert _order(page) == ["c-cheap", "b-mid", "a-dear"]
    assert "sort-asc" in page.get_attribute(_sort("price"), "class")
    assert page.get_attribute(_sort("price"), "aria-label") == "Sort by price, sorted cheapest first"
    assert page.get_attribute(_sort("name"), "aria-label") == "Sort by model, A to Z"
    page.click(_sort("price"))
    assert _order(page) == ["a-dear", "b-mid", "c-cheap"]
    assert page.get_attribute(_sort("price"), "aria-label") == "Sort by price, sorted most expensive first"
    # Context sorts high to low first; the model without one stays last.
    page.click(_sort("context"))
    assert _order(page) == ["a-dear", "b-mid", "c-cheap"]
    page.click(_sort("context"))
    assert _order(page) == ["b-mid", "a-dear", "c-cheap"]
    assert errors == []


def test_sorting_keeps_the_card_filter_and_the_url(priced_models, serve_models):
    page, errors = serve_models()
    page.click(_card("early-access"))
    page.click(_sort("price"))
    assert _visible(page) == ["c-cheap", "a-dear"]
    assert _pressed(page) == ["early-access"]
    assert _query(page) == {"show": "early_access", "sort": "price", "dir": "asc"}
    # The server renders the same order, filter and arrow on reload.
    page.reload()
    page.wait_for_selector("#model-list .model-row-toggle", state="attached")
    assert _visible(page) == ["c-cheap", "a-dear"]
    assert _pressed(page) == ["early-access"]
    assert "sort-asc" in page.get_attribute(_sort("price"), "class")
    # Changing the card keeps the sort.
    page.click(_card("models"))
    assert _order(page) == ["c-cheap", "b-mid", "a-dear"]
    assert _query(page) == {"sort": "price", "dir": "asc"}
    assert errors == []


def test_open_row_stays_open_after_sorting(priced_models, serve_models):
    page, errors = serve_models()
    page.click('li[data-model="a-dear"] .model-row-toggle')
    page.wait_for_function("!document.querySelector('#model-list .collapsing')")
    page.click(_sort("price"))
    assert _order(page)[-1] == "a-dear"
    assert page.eval_on_selector_all("#model-list .collapse.show", "els => els.map(e => e.closest('li').dataset.model)") == ["a-dear"]
    assert page.get_attribute('li[data-model="a-dear"] .model-row-toggle', "aria-expanded") == "true"
    # Opening another row still closes it: the single-open behaviour survives the move.
    page.click('li[data-model="c-cheap"] .model-row-toggle')
    page.wait_for_function("!document.querySelector('#model-list .collapsing')")
    assert page.eval_on_selector_all("#model-list .collapse.show", "els => els.map(e => e.closest('li').dataset.model)") == ["c-cheap"]
    assert errors == []


def test_sort_header_wraps_on_a_phone(priced_models, serve_models):
    page, errors = serve_models()
    page.set_viewport_size({"width": 375, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert all(page.is_visible(_sort(k)) for k in ("name", "context", "max_output", "price"))
    assert errors == []


def _reloaded_order(page):
    page.reload()
    page.wait_for_selector("#model-list .model-row-toggle", state="attached")
    return _order(page)


@pytest.mark.parametrize(("key", "field"), [("context", "context_window"), ("max_output", "max_output_tokens")])
def test_zero_token_limit_sorts_last_on_click_and_reload(app, serve_models, key, field):
    _add_model(app, "a-zero", **{field: 0})
    _add_model(app, "b-small", **{field: 8_000})
    _add_model(app, "c-unset", **{field: None})
    _add_model(app, "d-big", **{field: 128_000})
    page, errors = serve_models()
    for present in (["d-big", "b-small"], ["b-small", "d-big"]):  # first click high to low, then reversed
        page.click(_sort(key))
        assert _order(page) == [*present, "a-zero", "c-unset"]
        assert _reloaded_order(page) == [*present, "a-zero", "c-unset"]
    assert errors == []


def test_click_and_reload_agree_on_astral_and_full_width_names(app, serve_models):
    # JavaScript compares UTF-16 code units and Python code points: U+FF41 sorts
    # before U+10428 in Python but after its lead surrogate U+D801 in JavaScript.
    # Equal prices make the price sort fall back to the name A to Z, both ways.
    for name in ("\U00010428-model", "\uff41-model", "a-model"):
        _add_model(app, name)
    names = ["a-model", "\uff41-model", "\U00010428-model"]
    page, errors = serve_models()
    for key, order in (("name", names[::-1]), ("name", names), ("price", names), ("price", names)):
        page.click(_sort(key))
        assert _order(page) == order, key
        assert _reloaded_order(page) == order, key
    assert errors == []
