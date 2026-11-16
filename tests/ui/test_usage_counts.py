"""The /usage summary cards show request and token totals like the profile page.

The page formats them in the browser with its own compactNumber, a copy of the
Jinja filter compact_number. The page is served by the Flask test client through
page.route, as in test_usage_fetches.py, with /api/usage/summary stubbed.
"""
import json
from urllib.parse import urlsplit

import pytest

from lumen import compact_number
from tests.ui.test_key_tables import ORIGIN
from tests.ui.test_usage_fetches import CHART_STUB

SUMMARY = {"requests": 999_999, "tokens": 16_058_634_931, "cost": 12.5, "new_users": 3, "last_active": None}


@pytest.fixture
def usage_page(admin_client, browser):
    """Yield /usage as an admin, after the first refresh, with a stubbed summary."""

    def handle(route, request):
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()  # CDN assets: keep the test hermetic
            return
        if url.path == "/api/usage/summary":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(SUMMARY))
            return
        resp = admin_client.get(url.path + (f"?{url.query}" if url.query else ""))
        # Close the response: a static file stays open until it is.
        with resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    page = browser.new_page()
    page.set_default_timeout(10_000)
    page.add_init_script(CHART_STUB)
    page.route("**/*", handle)
    with page.expect_response(lambda r: urlsplit(r.url).path == "/api/usage/heatmap"):
        page.goto(f"{ORIGIN}/usage")
    page.wait_for_function("document.getElementById('stat-tokens').textContent !== '…'")
    yield page
    page.close()


def test_summary_cards_use_human_readable_counts(usage_page):
    assert usage_page.text_content("#stat-requests") == "999,999"
    assert usage_page.text_content("#stat-tokens") == "16.1 billion"
    assert usage_page.text_content("#stat-cost") == "12.50"


@pytest.mark.parametrize(
    "value",
    [None, 0, 499, 999_999, 1_000_000, 5_224_879, 999_950_000, 16_058_634_931,
     2_500_000_000_000, 1_234_567_000_000_000, -5_224_879,
     # Ties exact in binary: Python rounds them to even.
     2_250_000, 2_750_000, -2_250_000, 2_250_000_000, 2_750_000_000_000,
     # Ties that are not exact in binary: the double sits just above or below.
     1_150_000, 2_150_000, 2_350_000, 4_150_000, -2_150_000,
     1_150_000_000, 2_150_000_000, 1_150_000_000_000, 2_150_000_000_000,
     1_234_550_000_000_000],
)
def test_compact_number_matches_the_python_filter(usage_page, value):
    assert usage_page.evaluate("compactNumber", value) == compact_number(value)


def test_compact_number_matches_the_python_filter_on_every_tie(usage_page):
    """Every x.x5 tie from 1.05 to 999.95 million, billion and trillion."""
    values = [k * 50_000 * scale for scale in (1, 1_000, 1_000_000) for k in range(21, 20_000, 2)]
    got = usage_page.evaluate("values => values.map(compactNumber)", values)
    mismatches = [(v, g, compact_number(v)) for v, g in zip(values, got) if g != compact_number(v)]
    assert not mismatches[:10]
