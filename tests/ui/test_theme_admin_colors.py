"""The admin config nav and the group picker follow the active theme's primary colour.

Loads the real pages in Chromium, served by the Flask test client at
http://lumen.test like test_group_picker.py, once per theme, and compares the
computed colours with the --theme-primary each theme's head_extras.html sets.
"""
from contextlib import contextmanager
from http import HTTPStatus
from urllib.parse import urlsplit

import pytest

from tests.test_theme_colors import THEME_HEADS, THEME_PRIMARY
from tests.ui.test_group_picker import groups  # noqa: F401  (fixture)
from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN

THEMES = [
    pytest.param(head.parents[2].name, THEME_PRIMARY.search(head.read_text()).group(1), id=head.parents[2].name)
    for head in THEME_HEADS
]


def _rgb(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgb({r}, {g}, {b})"


@contextmanager
def _theme(app, name):
    old_theme, old_name = app.config["THEME"], app.config["THEME_NAME"]
    app.config["THEME"] = app.config["THEME_CACHE"][name]
    app.config["THEME_NAME"] = name
    try:
        yield
    finally:
        app.config["THEME"], app.config["THEME_NAME"] = old_theme, old_name


@pytest.fixture
def open_page(admin_client, browser):
    pages = []

    def handle(route, request):
        url = urlsplit(request.url)
        if request.url == BOOTSTRAP_JS_URL:
            route.fulfill(status=200, content_type="application/javascript",
                          headers={"Access-Control-Allow-Origin": "*"}, body=BOOTSTRAP_JS.read_bytes())
            return
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()
            return
        # Close the response: a static file stays open until it is.
        with admin_client.get(url.path + (f"?{url.query}" if url.query else "")) as resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    def _open(path, width=1200):
        page = browser.new_page(viewport={"width": width, "height": 800})
        pages.append(page)
        page.set_default_timeout(10_000)
        page.route("**/*", handle)
        assert page.goto(f"{ORIGIN}{path}").status == HTTPStatus.OK
        return page

    yield _open
    for page in pages:
        page.close()


def _style(page, selector, prop):
    return page.eval_on_selector(selector, f"e => getComputedStyle(e).{prop}")


@pytest.mark.parametrize(("theme", "primary"), THEMES)
def test_config_nav_active_button_uses_theme_primary(app, open_page, theme, primary):
    with _theme(app, theme):
        desktop = open_page("/admin/config")
        phone = open_page("/admin/config", width=600)
    active = ".cfg-nav-btn.active"
    desktop.wait_for_selector(active)
    assert _style(desktop, active, "color") == _rgb(primary)
    assert _style(desktop, active, "borderLeftColor") == _rgb(primary)
    phone.wait_for_selector(active)
    assert _style(phone, active, "borderBottomColor") == _rgb(primary)
    assert _style(phone, active, "borderBottomWidth") == "3px"


@pytest.mark.parametrize(("theme", "primary"), THEMES)
def test_group_suggestion_highlight_uses_theme_primary(app, open_page, test_user, groups, theme, primary):  # noqa: F811
    with _theme(app, theme):
        page = open_page(f"/admin/users/{test_user['uuid']}/profile")
    page.get_by_role("button", name="Edit").first.click()
    page.wait_for_selector("#editUserModal.show")
    page.fill("#edit-user-groups", "joi")
    page.keyboard.press("ArrowDown")
    active = "#edit-user-groups-list .active"
    page.wait_for_selector(active)
    assert _style(page, active, "backgroundColor") == _rgb(primary)
    assert _style(page, active, "color") == "rgb(255, 255, 255)"
