"""Run the Model Dashboard's Access & Acknowledgments list in a real browser.

The page is loaded in headless Chromium in a fixed timezone and locale, with
every same-origin request (POSTs included) answered by the Flask test client and
Bootstrap's JS served from the pinned copy, like test_models_row_click.py. The
Markdown renderer and sanitizer behind the acknowledgment dialog are replaced by
pass-through stubs so the test needs no network.
"""
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN

# The CDN copies are aborted (their SRI hashes would reject a stub), so the
# dialog's globals are defined before the page's own scripts run.
STUBS = "window.marked = { parse: s => s }; window.DOMPurify = { sanitize: s => s };"


@pytest.fixture
def models_page(app, auth_client, browser, test_user):
    """Open /models in America/Chicago with one model acknowledged and its early access pending,
    and one with both requirements pending."""
    with app.app_context():
        model = ModelConfig(model_name="ack-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                            needs_ack=True, early_access=True)
        db.session.add(model)
        db.session.add(ModelConfig(model_name="both-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                                   needs_ack=True, early_access=True, ack_message="Read the terms."))
        db.session.flush()
        # 04:05 UTC on Feb 3 is 22:05 on Feb 2 in Chicago (UTC-6).
        db.session.add(EntityModelConsent(entity_id=test_user["id"], model_config_id=model.id,
                                          consented_at=datetime(2026, 2, 3, 4, 5, 6)))
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
        path = url.path + (f"?{url.query}" if url.query else "")
        with auth_client.open(path, method=request.method, data=request.post_data_buffer,
                              headers={"Content-Type": request.headers.get("content-type", "")}) as resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    context = browser.new_context(timezone_id="America/Chicago", locale="en-US")
    page = context.new_page()
    page.set_default_timeout(10_000)
    page.add_init_script(STUBS)
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.route("**/*", handle)
    page.goto(f"{ORIGIN}/models")
    page.wait_for_selector("#model-list .model-row-toggle")
    yield page, errors
    context.close()


ROW = 'li[data-model="ack-model"]'


def _value(page, label):
    return page.locator(f"{ROW} .model-access-list dt:text-is('{label}') + dd")


def test_accepted_date_is_local_with_a_keyboard_tooltip(models_page):
    page, errors = models_page
    page.click(f"{ROW} .model-row-toggle")
    date = _value(page, "Acknowledgment").locator(".local-datetime")
    date.wait_for(state="visible")
    # Date only, in the browser's timezone: the previous day in Chicago.
    assert date.inner_text() == "Feb 2, 2026"
    assert date.get_attribute("title") is None  # Bootstrap moved it into the tooltip
    assert "10:05 PM" in date.get_attribute("data-bs-original-title")
    assert "02/02/2026" in date.get_attribute("data-bs-original-title")
    # Keyboard users reach the full timestamp: the date takes focus and opens its tooltip.
    assert date.get_attribute("tabindex") == "0"
    date.focus()
    tooltip = page.locator(".tooltip")
    tooltip.wait_for(state="visible")
    assert "10:05 PM" in tooltip.inner_text()
    assert errors == []


def test_accepting_early_access_shows_its_date_and_lowers_needs_ack(models_page):
    page, errors = models_page
    needs_ack = page.locator('.model-stat[data-stat="needs-ack"] .model-stat-count')
    assert needs_ack.inner_text() == "2"
    page.click(f"{ROW} .model-row-toggle")
    early = _value(page, "Early access")
    assert early.locator("button.ack-btn").inner_text() == "early access for ack-model"
    early.locator("button.ack-btn").click()
    page.locator("#ackModal").wait_for(state="visible")
    with page.expect_navigation():
        page.click("#ack-accept-btn")  # the page reloads after the consent is stored
    page.wait_for_selector("#model-list .model-row-toggle")
    assert needs_ack.inner_text() == "1"
    page.click(f"{ROW} .model-row-toggle")
    date = _value(page, "Early access").locator(".local-datetime")
    date.wait_for(state="visible")
    assert date.inner_text()  # today's date, in local time
    assert early.locator("button").count() == 0
    # The acknowledgment keeps its own, earlier date.
    assert _value(page, "Acknowledgment").inner_text() == "Feb 2, 2026"
    assert errors == []


BOTH = 'li[data-model="both-model"]'


def test_accept_and_enable_in_the_row_accepts_both(models_page):
    page, errors = models_page
    needs_ack = page.locator('.model-stat[data-stat="needs-ack"] .model-stat-count')
    assert needs_ack.inner_text() == "2"
    button = page.locator(f"{BOTH} .model-row-ack button.ack-btn")
    assert button.inner_text() == "Accept & Enable for both-model"
    button.click()
    dialog = page.locator("#ackModal")
    dialog.wait_for(state="visible")
    # The dialog lists both notices.
    assert page.locator("#ack-notice-section").is_visible()
    assert page.locator("#ack-early-section").is_visible()
    assert "Read the terms." in dialog.inner_text()
    with page.expect_navigation():
        page.click("#ack-accept-btn")
    page.wait_for_selector("#model-list .model-row-toggle")
    assert needs_ack.inner_text() == "1"
    assert page.locator(f"{BOTH} .model-row-ack").count() == 0
    page.click(f"{BOTH} .model-row-toggle")
    for label in ("Acknowledgment", "Early access"):
        value = page.locator(f"{BOTH} .model-access-list dt:text-is('{label}') + dd")
        value.locator(".local-datetime").wait_for(state="visible")
        assert value.get_attribute("data-state") == "accepted"
        assert value.locator("button").count() == 0
    assert errors == []
