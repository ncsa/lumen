"""The copy-ID avatar (templates/_copy_id_avatar.html, static/js/copy-id.js).

Markup checks run on the rendered project, group and profile pages; the behaviour runs in a real
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
    assert f"/projects/{pid}" not in html
    assert f"PROJECT_ID = {pid};" not in html


def _set_active(app, pid, active):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        db.session.get(Entity, pid).active = active
        db.session.commit()


@pytest.mark.parametrize("active,label", [(True, "Deactivate"), (False, "Activate")])
def test_toggle_button_handler_parses_with_the_uuid(app, auth_client, project, active, label):
    """The parsed onclick holds the whole call; double quotes inside a
    double-quoted attribute would cut it off at toggleProject(."""
    pid, puuid = project
    _set_active(app, pid, active)
    soup = BeautifulSoup(auth_client.get(f"/projects/{puuid}").data, "html.parser")
    btn = next(b for b in soup.select(".profile-col-identity button") if b.get_text(strip=True) == label)
    assert btn["onclick"] == f'toggleProject("{puuid}", {str(active).lower()})'
    assert len(btn.attrs) == 2  # class and onclick: no stray attributes from broken quoting


def _page_opener(auth_client, browser, path):
    """Open path in the browser, served by the Flask test client; returns (open, pages)."""
    pages = []
    posted = []

    def handle(route, request):
        url = urlsplit(request.url)
        if request.url == BOOTSTRAP_JS_URL:
            route.fulfill(status=200, content_type="application/javascript",
                          headers={"Access-Control-Allow-Origin": "*"}, body=BOOTSTRAP_JS.read_bytes())
            return
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()
            return
        req_path = url.path + (f"?{url.query}" if url.query else "")
        if request.method == "POST":
            posted.append(url.path)
            resp = auth_client.post(req_path, json=request.post_data_json)
        else:
            resp = auth_client.get(req_path)
        # Close the response: a static file stays open until it is.
        with resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    def _open():
        page = browser.new_page()
        pages.append(page)
        page.set_default_timeout(10_000)
        page.add_init_script(CLIPBOARD_STUB)
        page.route("**/*", handle)
        resp = page.goto(f"{ORIGIN}{path}")
        assert resp.status == HTTPStatus.OK
        page.wait_for_selector("button.copy-id-avatar")
        return page

    _open.posted = posted
    return _open, pages


@pytest.fixture
def open_project(auth_client, browser, project):
    _open, pages = _page_opener(auth_client, browser, f"/projects/{project[1]}")
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


def test_toggle_buttons_deactivate_and_reactivate(app, open_project, project):
    """Clicking Deactivate, then Activate, posts to the uuid toggle URL each time."""
    pid, puuid = project
    errors = []
    page = open_project()
    page.on("pageerror", lambda e: errors.append(str(e)))
    with page.expect_navigation():
        page.get_by_role("button", name="Deactivate").click()
    page.get_by_role("button", name="Activate").wait_for()
    with page.expect_navigation():
        page.get_by_role("button", name="Activate").click()
    page.get_by_role("button", name="Deactivate").wait_for()
    assert open_project.posted == [f"/projects/{puuid}/toggle"] * 2
    assert errors == []
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        assert db.session.get(Entity, pid).active is True


# --- Group avatar ----------------------------------------------------------

@pytest.fixture
def group(app, test_user):
    """A group owned by test_user; returns (id, uuid)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        g = Group(name="copy-group", active=True)
        db.session.add(g)
        db.session.flush()
        db.session.add(GroupMember(group_id=g.id, entity_id=test_user["id"], is_owner=True))
        db.session.commit()
        return g.id, str(g.uuid)


def test_group_avatar_is_a_labelled_copy_button(auth_client, group):
    gid, guuid = group
    soup = BeautifulSoup(auth_client.get(f"/groups/{guuid}").data, "html.parser")
    btn = _avatar(soup)
    assert btn is not None and btn["type"] == "button"
    assert btn["data-copy-id"] == guuid
    assert btn["aria-label"] == "Copy group ID of copy-group"
    assert btn.find("img")["alt"] == "Avatar for copy-group"
    status = btn.find_next_sibling("span")
    assert status["role"] == "status" and status["aria-live"] == "polite"
    assert soup.find("script", src="/static/js/copy-id.js") is not None


def test_group_page_embeds_no_integer_group_id(auth_client, group):
    gid, guuid = group
    html = auth_client.get(f"/groups/{guuid}").get_data(as_text=True)
    assert f'const GROUP_ID = "{guuid}";' in html
    assert f"/groups/{gid}" not in html
    assert f"GROUP_ID = {gid};" not in html


@pytest.fixture
def open_group(auth_client, browser, group):
    _open, pages = _page_opener(auth_client, browser, f"/groups/{group[1]}")
    yield _open
    for page in pages:
        page.close()


def test_group_avatar_keyboard_focus_shows_hint(open_group):
    page = open_group()
    page.focus("button.copy-id-avatar")
    assert _popover_text(page) == "Click to copy ID"
    page.keyboard.press("Escape")
    page.wait_for_selector(".popover", state="detached")


def test_group_avatar_hover_shows_hint(open_group):
    page = open_group()
    page.hover("button.copy-id-avatar")
    assert _popover_text(page) == "Click to copy ID"


def test_group_avatar_click_copies_uuid_and_confirms(open_group, group):
    page = open_group()
    page.click("button.copy-id-avatar")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == [group[1]]
    page.wait_for_function(
        "document.querySelector('.copy-id-status').textContent === 'Group ID copied to clipboard'")
    assert _popover_text(page) == "Group ID copied to clipboard"


def test_group_avatar_enter_key_copies_uuid(open_group, group):
    page = open_group()
    page.focus("button.copy-id-avatar")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == [group[1]]


# --- User avatar (own profile and the admin view of a user) ---------------

def test_own_profile_avatar_is_a_labelled_copy_button(auth_client, test_user):
    soup = BeautifulSoup(auth_client.get("/profile").data, "html.parser")
    btn = _avatar(soup)
    assert btn is not None and btn["type"] == "button"
    assert btn["data-copy-id"] == test_user["uuid"]
    assert btn["aria-label"] == f"Copy user ID of {test_user['name']}"
    assert btn.find("img")["alt"] == f"Avatar for {test_user['name']}"
    status = btn.find_next_sibling("span")
    assert status["role"] == "status" and status["aria-live"] == "polite"
    assert soup.find("script", src="/static/js/copy-id.js") is not None


def test_admin_view_of_a_user_avatar_copies_that_users_uuid(admin_client, test_user):
    html = admin_client.get(f"/admin/users/{test_user['uuid']}/profile").get_data(as_text=True)
    btn = _avatar(BeautifulSoup(html, "html.parser"))
    assert btn["data-copy-id"] == test_user["uuid"]
    assert btn["aria-label"] == f"Copy user ID of {test_user['name']}"
    assert f"/admin/users/{test_user['id']}/" not in html
    assert f"EDIT_USER_ID = {test_user['id']};" not in html


@pytest.fixture
def open_user_profile(admin_client, browser, test_user):
    _open, pages = _page_opener(admin_client, browser, f"/admin/users/{test_user['uuid']}/profile")
    yield _open
    for page in pages:
        page.close()


def test_user_avatar_keyboard_focus_shows_hint(open_user_profile):
    page = open_user_profile()
    page.focus("button.copy-id-avatar")
    assert _popover_text(page) == "Click to copy ID"


def test_user_avatar_hover_shows_hint(open_user_profile):
    page = open_user_profile()
    page.hover("button.copy-id-avatar")
    assert _popover_text(page) == "Click to copy ID"


def test_user_avatar_click_copies_uuid_and_confirms(open_user_profile, test_user):
    page = open_user_profile()
    page.click("button.copy-id-avatar")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == [test_user["uuid"]]
    page.wait_for_function(
        "document.querySelector('.copy-id-status').textContent === 'User ID copied to clipboard'")
    assert _popover_text(page) == "User ID copied to clipboard"


def test_user_avatar_enter_key_copies_uuid(open_user_profile, test_user):
    page = open_user_profile()
    page.focus("button.copy-id-avatar")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == [test_user["uuid"]]
