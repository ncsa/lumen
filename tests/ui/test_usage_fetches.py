"""The /usage page fetches the user-growth charts only when it shows them.

Both endpoints group every entity by creation date and ignore the mine and
entity_id filters, so in the filtered view (the default "my usage") their
result used to be computed and then hidden. The page is served by the Flask
test client through page.route, like test_key_tables.py. Chart.js, which the
page loads from its CDN, is replaced by a stub: these tests only watch requests.
"""
from urllib.parse import urlsplit

import pytest

from tests.ui.test_key_tables import ORIGIN

CHART_STUB = """
window.Chart = class {
  constructor(ctx, config) { this.data = config.data; }
  update() {}
};
"""
USER_GROWTH = {"/api/usage/users/new", "/api/usage/users/cumulative"}


@pytest.fixture
def usage_page(admin_client, browser):
    """Yield (page, requested API paths) for /usage as an admin, after the first refresh."""
    requested = []

    def handle(route, request):
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()  # CDN assets: keep the test hermetic
            return
        if url.path.startswith("/api/usage/"):
            requested.append(url.path)
        resp = admin_client.get(url.path + (f"?{url.query}" if url.query else ""))
        route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    page = browser.new_page()
    page.set_default_timeout(10_000)
    page.add_init_script(CHART_STUB)
    page.route("**/*", handle)
    with page.expect_response(lambda r: urlsplit(r.url).path == "/api/usage/heatmap"):
        page.goto(f"{ORIGIN}/usage")
    yield page, requested
    page.close()


def test_filtered_view_skips_the_user_growth_fetches(usage_page):
    page, requested = usage_page
    assert "/api/usage/summary" in requested
    assert not USER_GROWTH & set(requested)
    assert "d-none" in page.get_attribute("#user-growth-row", "class")


def test_all_users_view_fetches_the_user_growth_charts(usage_page):
    page, requested = usage_page
    requested.clear()
    with page.expect_response(lambda r: urlsplit(r.url).path == "/api/usage/heatmap"):
        page.check("#all-users-check")
    assert USER_GROWTH <= set(requested)
    assert "d-none" not in page.get_attribute("#user-growth-row", "class")
