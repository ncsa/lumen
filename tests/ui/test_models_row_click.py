"""Run the Model Dashboard's row click in a real browser.

A click anywhere on a model row's summary toggles its details through the row's
chevron button. These tests load /models in headless Chromium with every
same-origin request answered by the Flask test client, and Bootstrap's JS served
from the pinned copy, like test_key_tables.py, so they need no network.
"""
from urllib.parse import urlsplit

import pytest

from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN
from tests.ui.test_models_access_browser import STUBS

MODELS = ("alpha-model", "beta-model")


@pytest.fixture
def models_page(app, auth_client, browser):
    """Open /models with two models; return (page, uncaught page errors)."""
    with app.app_context():
        for name in MODELS:
            # beta-model is early access, so its row shows the lock and flask icons
            # and an early access button.
            db.session.add(ModelConfig(model_name=name, input_cost_per_million=1.0, output_cost_per_million=2.0,
                                       early_access=name == "beta-model"))
        db.session.commit()

    def handle(route, request):
        url = urlsplit(request.url)
        if request.url == BOOTSTRAP_JS_URL:
            route.fulfill(status=200, content_type="application/javascript",
                          headers={"Access-Control-Allow-Origin": "*"}, body=BOOTSTRAP_JS.read_bytes())
            return
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()  # other CDN assets: keep the test hermetic
            return
        with auth_client.get(url.path + (f"?{url.query}" if url.query else "")) as resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    page = browser.new_page()
    page.set_default_timeout(10_000)
    page.add_init_script(STUBS)  # the acknowledgment dialog's Markdown renderer and sanitizer
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.route("**/*", handle)
    page.goto(f"{ORIGIN}/models")
    page.wait_for_selector("#model-list .model-row-toggle")
    yield page, errors
    page.close()


def _row(name):
    return f'li[data-model="{name}"]'


def _open_rows(page):
    """Names of the rows whose details are open, once Bootstrap's transition settles."""
    page.wait_for_function("!document.querySelector('#model-list .collapsing')")
    return page.eval_on_selector_all("#model-list .collapse.show", "els => els.map(e => e.closest('li').dataset.model)")


def test_clicking_a_row_summary_opens_switches_and_closes(models_page):
    page, errors = models_page
    page.click(f"{_row('alpha-model')} .model-limits")
    assert _open_rows(page) == ["alpha-model"]
    assert page.get_attribute(f"{_row('alpha-model')} .model-row-toggle", "aria-expanded") == "true"

    page.click(f"{_row('beta-model')} .model-price")
    assert _open_rows(page) == ["beta-model"]

    page.click(f"{_row('beta-model')} .model-modalities")
    assert _open_rows(page) == []
    assert errors == []


def test_column_labels_are_not_clickable_rows(models_page):
    page, errors = models_page
    for label in page.query_selector_all(".model-list-columns > span"):
        label.click()
    assert _open_rows(page) == []
    assert errors == []
    assert page.eval_on_selector(".model-list-columns", "el => getComputedStyle(el).cursor") != "pointer"
    assert page.eval_on_selector(f"{_row('alpha-model')} .model-row-grid", "el => getComputedStyle(el).cursor") == "pointer"


def test_chevron_toggles_once(models_page):
    page, errors = models_page
    page.click(f"{_row('alpha-model')} .model-row-toggle")
    assert _open_rows(page) == ["alpha-model"]
    page.click(f"{_row('alpha-model')} .model-row-toggle")
    assert _open_rows(page) == []
    assert errors == []


def test_keyboard_on_the_chevron_keeps_one_row_open(models_page):
    page, errors = models_page
    page.focus(f"{_row('alpha-model')} .model-row-toggle")
    page.keyboard.press("Enter")
    assert _open_rows(page) == ["alpha-model"]
    page.focus(f"{_row('beta-model')} .model-row-toggle")
    page.keyboard.press("Space")
    assert _open_rows(page) == ["beta-model"]
    page.keyboard.press("Enter")
    assert _open_rows(page) == []
    assert errors == []


def test_clicking_inside_an_open_panel_does_not_close_it(models_page):
    page, errors = models_page
    page.click(f"{_row('alpha-model')} .model-limits")
    assert _open_rows(page) == ["alpha-model"]
    page.click(f"{_row('alpha-model')} .model-pricing")
    assert _open_rows(page) == ["alpha-model"]
    assert errors == []


def test_row_ack_button_opens_the_dialog_without_toggling_the_row(models_page):
    page, errors = models_page
    button = page.locator(f"{_row('beta-model')} .model-row-ack button.ack-btn")
    assert button.inner_text() == "early access for beta-model"
    button.click()
    page.locator("#ackModal").wait_for(state="visible")
    assert _open_rows(page) == []
    assert page.get_attribute(f"{_row('beta-model')} .model-row-toggle", "aria-expanded") == "false"
    assert errors == []


def test_model_name_still_opens_the_detail_page(models_page):
    page, errors = models_page
    page.click(f"{_row('alpha-model')} a.model-name")
    page.wait_for_url(f"{ORIGIN}/models/alpha-model")
    assert errors == []


def _select_text(page, selector):
    page.evaluate("""sel => {
        const range = document.createRange();
        range.selectNodeContents(document.querySelector(sel));
        getSelection().removeAllRanges();
        getSelection().addRange(range);
    }""", selector)


def _click_without_mouse(page, selector):
    # A real mouse-down would clear the selection first; dispatch the click alone.
    page.eval_on_selector(selector, "el => el.dispatchEvent(new MouseEvent('click', {bubbles: true}))")


def test_selecting_text_in_a_row_does_not_toggle_it(models_page):
    page, errors = models_page
    _select_text(page, f"{_row('alpha-model')} .model-context")
    _click_without_mouse(page, f"{_row('alpha-model')} .model-context")
    assert _open_rows(page) == []
    assert errors == []


def test_a_selection_elsewhere_does_not_block_the_row(models_page):
    page, errors = models_page
    _select_text(page, "h1")
    _click_without_mouse(page, f"{_row('beta-model')} .model-context")
    assert _open_rows(page) == ["beta-model"]
    assert errors == []


def test_lock_and_flask_sit_inline_and_smaller_than_the_name(models_page):
    page, errors = models_page
    sizes = page.eval_on_selector(_row("beta-model"), """row => {
        const name = row.querySelector('a.model-name').getBoundingClientRect();
        return [...row.querySelectorAll('.model-flag')].map(icon => {
            const box = icon.getBoundingClientRect();
            return {
                label: icon.getAttribute('aria-label'),
                smaller: parseFloat(getComputedStyle(icon).fontSize) < parseFloat(getComputedStyle(row.querySelector('a.model-name')).fontSize),
                inline: box.top >= name.top - 1 && box.bottom <= name.bottom + 1,
            };
        });
    }""")
    assert sizes == [
        {"label": "Needs acknowledgment", "smaller": True, "inline": True},
        {"label": "Early access", "smaller": True, "inline": True},
    ]
    assert errors == []
