"""The copy-ID avatar (templates/_copy_id_avatar.html, static/js/copy-id.js).

Markup checks run on the rendered project page; the behaviour runs in a real
browser, served by the Flask test client at http://lumen.test like
test_key_tables.py. That origin is not a secure context, so an init script
stubs navigator.clipboard and records what was written in window.__copied.
"""
from http import HTTPStatus
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from tests.conftest import make_project
from tests.ui.test_chat_copy import CLIPBOARD_STUB
from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN


@pytest.fixture
def project(app, test_user):
    """A project owned by test_user; returns (id, uuid)."""
    with app.app_context():
        p = make_project("copy-svc", owner_id=test_user["id"], initials="CS")
        return p.id, str(p.uuid)


def _avatar(soup):
    return soup.select_one("button.copy-id-avatar")


def test_project_avatar_is_a_labelled_copy_button(auth_client, project):
    pid, puuid = project
    html = auth_client.get(f"/projects/{puuid}").get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    btn = _avatar(soup)
    assert btn is not None and btn["type"] == "button"
    assert btn["data-copy-id"] == puuid
    assert btn["aria-label"] == "Copy project ID of copy-svc"
    assert btn.find("img")["alt"] == "Avatar for copy-svc"
    status = btn.find_next_sibling("span")
    assert "copy-id-status" in status["class"]
    assert status["role"] == "status" and status["aria-live"] == "polite"
    assert soup.find("script", src="/static/js/copy-id.js") is not None


def test_project_page_embeds_no_integer_project_id(auth_client, project):
    pid, puuid = project
    html = auth_client.get(f"/projects/{puuid}").get_data(as_text=True)
    assert f'const PROJECT_ID = "{puuid}";' in html
    assert f'toggleProject("{puuid}"' in html
    assert f"/projects/{pid}" not in html
    assert f"PROJECT_ID = {pid};" not in html


@pytest.fixture
def open_project(auth_client, browser, project):
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
        resp = auth_client.get(url.path + (f"?{url.query}" if url.query else ""))
        route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    def _open():
        page = browser.new_page()
        pages.append(page)
        page.set_default_timeout(10_000)
        page.add_init_script(CLIPBOARD_STUB)
        page.route("**/*", handle)
        resp = page.goto(f"{ORIGIN}/projects/{project[1]}")
        assert resp.status == HTTPStatus.OK
        page.wait_for_selector("button.copy-id-avatar")
        return page

    yield _open
    for page in pages:
        page.close()


def _popover_text(page):
    page.wait_for_selector(".popover .popover-body")
    return page.inner_text(".popover .popover-body")


def test_hover_shows_hint(open_project):
    page = open_project()
    page.hover("button.copy-id-avatar")
    assert _popover_text(page) == "Click to copy ID"


def test_keyboard_focus_shows_hint_and_escape_hides_it(open_project):
    page = open_project()
    page.focus("button.copy-id-avatar")
    assert _popover_text(page) == "Click to copy ID"
    page.keyboard.press("Escape")
    page.wait_for_selector(".popover", state="detached")


def test_click_copies_uuid_and_confirms(open_project, project):
    page = open_project()
    page.click("button.copy-id-avatar")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == [project[1]]
    page.wait_for_function(
        "document.querySelector('.copy-id-status').textContent === 'Project ID copied to clipboard'")
    assert _popover_text(page) == "Project ID copied to clipboard"


def test_enter_key_copies_uuid(open_project, project):
    page = open_project()
    page.focus("button.copy-id-avatar")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == [project[1]]


def test_copy_failure_is_announced(open_project):
    page = open_project()
    page.evaluate("window.__clipboardFails = true")
    page.click("button.copy-id-avatar")
    page.wait_for_function("document.querySelector('.copy-id-status').textContent === 'Copy failed'")
