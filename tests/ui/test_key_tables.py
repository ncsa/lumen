"""Run the API key tables and New API Key modals in a real browser.

The key tables are rendered client-side, so the Expires column and the
expired/revoked badges only exist after the page's JavaScript runs. These tests
load /profile and /projects/<sid> in headless Chromium, with every same-origin
request answered by the Flask test client (no live server). Bootstrap's JS,
which base.html loads from its CDN, is served from a pinned copy in
tests/fixtures/vendor, so the tests need no network. They check the rendered
tables with the accessibility checks and drive the New API Key save path.
"""
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, APIKey
from lumen.timeutils import utcnow
from tests.conftest import make_project
from tests.ui.test_accessibility import _run_all_checks

ORIGIN = "http://lumen.test"
# Must match the bundle base.html loads; the copy is that file, byte for byte.
BOOTSTRAP_JS_URL = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"
BOOTSTRAP_JS = Path(__file__).parent.parent / "fixtures" / "vendor" / "bootstrap-5.3.8.bundle.min.js"
DAY = 86400


def _page_opener(client, browser):
    """Return (open(path) -> (page, posted payloads), pages), answering requests with client."""
    pages = []
    posted = []

    def handle(route, request):
        url = urlsplit(request.url)
        if request.url == BOOTSTRAP_JS_URL:
            # The tables and modals need Bootstrap's JS; serve the pinned copy.
            route.fulfill(
                status=200,
                content_type="application/javascript",
                # base.html loads it with crossorigin + integrity, so it needs CORS.
                headers={"Access-Control-Allow-Origin": "*"},
                body=BOOTSTRAP_JS.read_bytes(),
            )
            return
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()  # other CDN assets: keep the test hermetic
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        if request.method == "POST":
            posted.append((url.path, request.post_data_json))
            resp = client.post(path, json=request.post_data_json)
        else:
            resp = client.get(path)
        # Close the response: a static file stays open until it is.
        with resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    def _open(path):
        page = browser.new_page()
        pages.append(page)
        page.set_default_timeout(10_000)
        page.route("**/*", handle)
        page.goto(f"{ORIGIN}{path}")
        page.wait_for_selector("#key-tbody tr")
        return page, posted

    return _open, pages


@pytest.fixture
def open_page(auth_client, browser):
    """Return open(path) -> (page, posted payloads), answering requests with auth_client."""
    _open, pages = _page_opener(auth_client, browser)
    yield _open
    for page in pages:
        page.close()


@pytest.fixture
def admin_open_page(admin_client, browser):
    """Like open_page, as an admin in admin mode."""
    _open, pages = _page_opener(admin_client, browser)
    yield _open
    for page in pages:
        page.close()


def test_base_template_loads_the_pinned_bootstrap():
    base = (Path(__file__).parent.parent.parent / "lumen" / "templates" / "base.html").read_text()
    assert f'src="{BOOTSTRAP_JS_URL}"' in base, "update BOOTSTRAP_JS_URL and the copy in tests/fixtures/vendor"


def _seed_keys(app, entity_id, creator_id):
    """A live expiring key, a never-expiring key, an expired key and a manually revoked one."""
    now = utcnow().replace(microsecond=0)
    expired_at = now - timedelta(days=1)
    with app.app_context():
        db.session.add_all([
            APIKey(entity_id=entity_id, created_by_entity_id=creator_id, name="live-key",
                   key_hash="1" * 64, key_hint="sk_live...0001", expires_at=now + timedelta(days=3)),
            APIKey(entity_id=entity_id, created_by_entity_id=creator_id, name="forever-key",
                   key_hash="2" * 64, key_hint="sk_ever...0002"),
            # Revoked by the expirer: no revoker, revoked_at == expires_at.
            APIKey(entity_id=entity_id, created_by_entity_id=creator_id, name="expired-key",
                   key_hash="3" * 64, key_hint="sk_expd...0003", expires_at=expired_at, revoked_at=expired_at),
            # Revoked by a user at its expiry time: still "revoked".
            APIKey(entity_id=entity_id, created_by_entity_id=creator_id, name="revoked-key",
                   key_hash="4" * 64, key_hint="sk_revk...0004", expires_at=expired_at, revoked_at=expired_at,
                   revoked_by_entity_id=creator_id),
        ])
        db.session.commit()


def _row_cells(page, name):
    """Cell texts of the key row named `name`."""
    row = page.locator("#key-tbody tr", has=page.locator(f"text={name}"))
    return [t.strip() for t in row.locator("td").all_inner_texts()]


def _project_for(app, owner_id):
    with app.app_context():
        project = make_project("ttl-ui", owner_id=owner_id)
        return project.id, project.uuid


@pytest.fixture(params=["profile", "project"])
def key_page(request, app, test_user):
    """(path, key POST path, Expires column index) for the profile or a project owned by test_user."""
    if request.param == "profile":
        _seed_keys(app, test_user["id"], test_user["id"])
        return "/profile", "/profile/keys", 5
    sid, sid_uuid = _project_for(app, test_user["id"])
    _seed_keys(app, sid, test_user["id"])
    # The owner sees the Created By column, so Expires moves one to the right.
    return f"/projects/{sid_uuid}#keys", f"/projects/{sid_uuid}/keys", 6


def test_key_table_expires_column_and_badges(open_page, key_page):
    path, _, expires_idx = key_page
    page, _ = open_page(path)

    # Revoked and expired keys are hidden until the toggle is on.
    names = page.locator("#key-tbody tr td:first-child").all_inner_texts()
    assert sorted(n.strip() for n in names) == ["forever-key", "live-key"]
    assert _row_cells(page, "forever-key")[expires_idx] == "Never"
    live_expires = _row_cells(page, "live-key")[expires_idx]
    assert live_expires not in ("", "Never", "—")

    # Expires sorts like Last Used: descending on first click, no value last.
    page.click("#key-table th[data-col='expires_at']")
    names = page.locator("#key-tbody tr td:first-child").all_inner_texts()
    assert [n.strip() for n in names] == ["live-key", "forever-key"]

    page.check("#show-revoked-cb")
    assert page.locator("#key-tbody tr").count() == 4
    assert _row_cells(page, "expired-key")[-1] == "expired"
    assert _row_cells(page, "revoked-key")[-1] == "revoked"
    assert _row_cells(page, "expired-key")[expires_idx] not in ("", "Never", "—")
    _run_all_checks(page.content().encode(), f"{path} (revoked and expired keys shown)")

    page.uncheck("#show-revoked-cb")
    assert page.locator("#key-tbody tr").count() == 2


@pytest.mark.parametrize("cap, options, selected", [
    (30 * DAY, ["1 day", "7 days", "30 days"], "30 days"),
    (0, ["1 day", "7 days", "30 days", "90 days", "1 year", "Never"], "Never"),
    (12 * 3600, ["12 hours"], "12 hours"),
])
def test_new_key_modal_ttl_dropdown(app, restore_config, open_page, key_page, cap, options, selected):
    app.config["KEY_TTL"] = cap
    path, post_path, _ = key_page
    page, posted = open_page(path)

    page.click("button[data-bs-target='#apiKeyModal']")
    page.wait_for_selector("#apiKeyModal.show")
    page.wait_for_function("document.getElementById('generated-key').value !== ''")
    assert [t.strip() for t in page.locator("#key-ttl-select option").all_inner_texts()] == options
    assert page.locator("#key-ttl-select option:checked").inner_text().strip() == selected
    _run_all_checks(page.content().encode(), f"{path} (New API Key modal open)")

    page.select_option("#key-ttl-select", label=options[0])
    chosen = int(page.input_value("#key-ttl-select"))
    page.fill("#key-name-input", "chosen-ttl")
    with page.expect_navigation():
        page.click("#save-key-btn")

    body = next(b for p, b in posted if p == post_path)
    # The profile always sends the key type (normal for everyone but admins in admin mode).
    expected = {"name": "chosen-ttl", "key": body["key"], "ttl": chosen}
    if post_path == "/profile/keys":
        expected["type"] = "normal"
    assert body == expected
    assert chosen == ({"1 day": DAY, "12 hours": 12 * 3600}[options[0]])
    with app.app_context():
        key = db.session.execute(select(APIKey).filter_by(name="chosen-ttl")).scalar_one()
        assert abs(key.expires_at - (utcnow() + timedelta(seconds=chosen))) < timedelta(minutes=1)


def _focused_ttl_button_id(page):
    return page.evaluate("document.activeElement.matches('.ttl-key-btn') ? document.activeElement.dataset.id : null")


def _wait_for_ttl_modal_closed(page):
    page.wait_for_selector("#keyTtlModal", state="hidden")
    # hidden alone is not enough: Bootstrap fires hidden.bs.modal (which restores focus) after the backdrop fade.
    page.wait_for_function("!document.body.classList.contains('modal-open')")


def test_edit_expiry_modal(app, restore_config, open_page, key_page):
    app.config["KEY_TTL"] = 30 * DAY
    path, post_path, expires_idx = key_page
    page, posted = open_page(path)
    with app.app_context():
        # The integer id is for DB checks; the page, its buttons and URLs carry the uuid.
        kid, kuid = db.session.execute(select(APIKey.id, APIKey.uuid).filter_by(name="live-key")).one()
        kuid = str(kuid)
    button = page.locator(f".ttl-key-btn[data-id='{kuid}']")
    assert button.get_attribute("aria-label") == "Edit expiry of key live-key"
    # Only live rows get the button.
    page.check("#show-revoked-cb")
    assert page.locator(".ttl-key-btn").count() == 2
    page.uncheck("#show-revoked-cb")

    # Cancelling returns focus to the row's button.
    button.click()
    page.wait_for_selector("#keyTtlModal.show")
    page.wait_for_function("document.activeElement.id === 'edit-key-ttl-select'")
    assert page.inner_text("#key-ttl-name") == "live-key"
    assert [t.strip() for t in page.locator("#edit-key-ttl-select option").all_inner_texts()] == \
        ["1 day", "7 days", "30 days"]
    assert page.locator("#edit-key-ttl-select option:checked").inner_text().strip() == "30 days"
    _run_all_checks(page.content().encode(), f"{path} (Edit Key Expiry modal open)")
    page.keyboard.press("Escape")
    _wait_for_ttl_modal_closed(page)
    assert _focused_ttl_button_id(page) == kuid

    # Saving updates the row in place and returns focus to the re-rendered button.
    before = _row_cells(page, "live-key")[expires_idx]
    page.locator(f".ttl-key-btn[data-id='{kuid}']").click()
    page.wait_for_selector("#keyTtlModal.show")
    page.select_option("#edit-key-ttl-select", label="7 days")
    page.click("#key-ttl-save-btn")
    _wait_for_ttl_modal_closed(page)
    assert posted[-1] == (f"{post_path}/{kuid}/ttl", {"ttl": 7 * DAY})
    assert _row_cells(page, "live-key")[expires_idx] != before
    assert _focused_ttl_button_id(page) == kuid
    with app.app_context():
        expires_at = db.session.get(APIKey, kid).expires_at
    assert abs(expires_at - (utcnow() + timedelta(days=7))) < timedelta(minutes=1)


def test_edit_expiry_modal_shows_server_error(app, restore_config, open_page, key_page):
    path, post_path, _ = key_page
    page, _ = open_page(path)
    with app.app_context():
        key = db.session.execute(select(APIKey).filter_by(name="live-key")).scalar_one()
        kuid = str(key.uuid)  # the page, its buttons and URLs carry the uuid
        key.revoked_at = utcnow()  # revoked in another tab
        db.session.commit()
    page.locator(f".ttl-key-btn[data-id='{kuid}']").click()
    page.wait_for_selector("#keyTtlModal.show")
    page.click("#key-ttl-save-btn")
    page.wait_for_function("document.getElementById('key-ttl-error').textContent !== ''")
    assert page.inner_text("#key-ttl-error") == "Error: Key is revoked"
    assert page.locator("#keyTtlModal.show").count() == 1


def test_edit_expiry_closed_while_saving_still_updates_row(app, restore_config, open_page, key_page):
    app.config["KEY_TTL"] = 30 * DAY
    path, _, expires_idx = key_page
    page, _ = open_page(path)
    with app.app_context():
        # The page, its buttons and URLs carry the uuid.
        kuid = str(db.session.execute(select(APIKey.uuid).filter_by(name="live-key")).scalar_one())
    held = []
    page.route("**/ttl", lambda route, request: held.append(route))  # hold the POST until the dialog is closed

    before = _row_cells(page, "live-key")[expires_idx]
    page.locator(f".ttl-key-btn[data-id='{kuid}']").click()
    page.wait_for_selector("#keyTtlModal.show")
    # .show is set when the fade-in starts; Bootstrap ignores Escape until shown.bs.modal (which focuses the select).
    page.wait_for_function("document.activeElement.id === 'edit-key-ttl-select'")
    page.select_option("#edit-key-ttl-select", label="1 day")
    page.click("#key-ttl-save-btn")
    page.wait_for_function("document.getElementById('key-ttl-save-btn').disabled")
    # Disabling the focused Save button lets Chrome move focus to <body>, where Bootstrap
    # does not see Escape; press it on the dialog itself.
    page.locator("#keyTtlModal").press("Escape")
    _wait_for_ttl_modal_closed(page)
    errors = []
    page.on("pageerror", lambda e: errors.append(e))
    held[0].fallback()  # the original handler answers it now

    page.wait_for_function("!document.getElementById('key-ttl-save-btn').disabled")
    assert errors == []
    assert _row_cells(page, "live-key")[expires_idx] != before
    assert _focused_ttl_button_id(page) == kuid


def test_edit_expiry_transport_error_shows_feedback(app, restore_config, open_page, key_page):
    """A save whose request never gets a response tells the user and re-enables Save."""
    path, _, _ = key_page
    page, _ = open_page(path)
    with app.app_context():
        # The page, its buttons and URLs carry the uuid.
        kuid = str(db.session.execute(select(APIKey.uuid).filter_by(name="live-key")).scalar_one())
    page.route("**/ttl", lambda route, request: route.abort("internetdisconnected"))
    errors = []
    page.on("pageerror", lambda e: errors.append(e))

    page.locator(f".ttl-key-btn[data-id='{kuid}']").click()
    page.wait_for_selector("#keyTtlModal.show")
    page.click("#key-ttl-save-btn")
    page.wait_for_function("document.getElementById('key-ttl-error').textContent !== ''")
    assert "Could not reach the server" in page.inner_text("#key-ttl-error")
    assert page.locator("#keyTtlModal.show").count() == 1
    assert page.is_enabled("#key-ttl-save-btn")
    assert errors == []


@pytest.mark.parametrize("failure", ["http", "transport"])
def test_edit_expiry_error_after_close_alerts(app, restore_config, open_page, key_page, failure):
    """A save that fails after its dialog was closed reports through the page's alert dialog."""
    path, _, expires_idx = key_page
    page, _ = open_page(path)
    with app.app_context():
        key = db.session.execute(select(APIKey).filter_by(name="live-key")).scalar_one()
        kid, kuid = key.id, str(key.uuid)
    held = []
    page.route("**/ttl", lambda route, request: held.append(route))

    before = _row_cells(page, "live-key")[expires_idx]
    page.locator(f".ttl-key-btn[data-id='{kuid}']").click()
    page.wait_for_selector("#keyTtlModal.show")
    page.wait_for_function("document.activeElement.id === 'edit-key-ttl-select'")  # shown: Escape now closes it
    page.click("#key-ttl-save-btn")
    page.wait_for_function("document.getElementById('key-ttl-save-btn').disabled")
    page.locator("#keyTtlModal").press("Escape")
    _wait_for_ttl_modal_closed(page)
    if failure == "http":
        with app.app_context():
            db.session.get(APIKey, kid).revoked_at = utcnow()  # revoked meanwhile: the server answers 409
            db.session.commit()
        held[0].fallback()
        expected = "Error: Key is revoked"
    else:
        held[0].abort("connectionreset")
        expected = "Could not reach the server"

    page.wait_for_function("!document.getElementById('key-ttl-save-btn').disabled")
    page.wait_for_selector(".modal.show")
    assert expected in page.locator(".modal.show .modal-body").inner_text()
    assert page.inner_text("#key-ttl-error") == ""
    assert _row_cells(page, "live-key")[expires_idx] == before


# --- key types (admin mode) -------------------------------------------------

def _ttl_labels(page, select):
    return [t.strip() for t in page.locator(f"{select} option").all_inner_texts()]


def test_new_key_modal_key_type_swaps_prefix_and_expiry(app, restore_config, admin_open_page, admin_user):
    app.config["KEY_TTL"] = 30 * DAY
    _seed_keys(app, admin_user["id"], admin_user["id"])
    page, posted = admin_open_page("/profile")

    page.click("button[data-bs-target='#apiKeyModal']")
    page.wait_for_selector("#apiKeyModal.show")
    page.wait_for_function("document.getElementById('generated-key').value !== ''")
    assert page.input_value("#key-type-select") == "normal"
    assert _ttl_labels(page, "#key-ttl-select") == ["1 day", "7 days", "30 days"]
    _run_all_checks(page.content().encode(), "/profile (New API Key modal, admin mode)")

    page.select_option("#key-type-select", "monitoring")
    page.wait_for_function("document.getElementById('generated-key').value.startsWith('sk_monitor_')")
    assert _ttl_labels(page, "#key-ttl-select") == ["1 day", "7 days", "30 days", "90 days", "1 year", "Never"]
    assert page.locator("#key-ttl-select option:checked").inner_text().strip() == "Never"

    page.select_option("#key-type-select", "admin")
    page.wait_for_function("document.getElementById('generated-key').value.startsWith('sk_admin_')")
    assert _ttl_labels(page, "#key-ttl-select") == ["1 day", "7 days"]
    assert page.locator("#key-ttl-select option:checked").inner_text().strip() == "7 days"
    page.fill("#key-name-input", "ops-admin")
    with page.expect_navigation():
        page.click("#save-key-btn")

    body = next(b for p, b in posted if p == "/profile/keys")
    assert body["type"] == "admin" and body["ttl"] == 7 * DAY and body["key"].startswith("sk_admin_")
    with app.app_context():
        key = db.session.execute(select(APIKey).filter_by(name="ops-admin")).scalar_one()
        assert key.key_type == KEY_TYPE_ADMIN

    # Reopening the dialog starts over with a normal key.
    page.wait_for_selector("#key-tbody tr")
    page.click("button[data-bs-target='#apiKeyModal']")
    page.wait_for_selector("#apiKeyModal.show")
    page.wait_for_function("document.getElementById('generated-key').value !== ''")
    assert page.input_value("#key-type-select") == "normal"
    assert not page.input_value("#generated-key").startswith(("sk_admin_", "sk_monitor_"))


def test_key_table_type_badge_and_edit_expiry_options(app, restore_config, admin_open_page, admin_user):
    app.config["KEY_TTL"] = 30 * DAY
    now = utcnow()
    with app.app_context():
        db.session.add_all([
            APIKey(entity_id=admin_user["id"], created_by_entity_id=admin_user["id"], name="adm-key",
                   key_hash="5" * 64, key_hint="sk_admin_abcd...0005", key_type=KEY_TYPE_ADMIN,
                   expires_at=now + timedelta(days=2)),
            APIKey(entity_id=admin_user["id"], created_by_entity_id=admin_user["id"], name="mon-key",
                   key_hash="6" * 64, key_hint="sk_monitor_abcd...0006", key_type=KEY_TYPE_MONITORING),
            APIKey(entity_id=admin_user["id"], created_by_entity_id=admin_user["id"], name="plain-key",
                   key_hash="7" * 64, key_hint="sk_abcd...0007"),
        ])
        db.session.commit()
        adm_uuid = str(db.session.execute(select(APIKey.uuid).filter_by(name="adm-key")).scalar_one())
    page, _ = admin_open_page("/profile")

    assert _row_cells(page, "adm-key")[0] == "adm-key Admin"
    assert _row_cells(page, "mon-key")[0] == "mon-key Monitoring"
    assert _row_cells(page, "plain-key")[0] == "plain-key"
    _run_all_checks(page.content().encode(), "/profile (key type badges)")

    # Edit expiry offers the key's own type's options.
    page.locator(f".ttl-key-btn[data-id='{adm_uuid}']").click()
    page.wait_for_selector("#keyTtlModal.show")
    assert _ttl_labels(page, "#edit-key-ttl-select") == ["1 day", "7 days"]
    page.keyboard.press("Escape")
    _wait_for_ttl_modal_closed(page)
    page.locator(".ttl-key-btn[data-name='plain-key']").click()
    page.wait_for_selector("#keyTtlModal.show")
    assert _ttl_labels(page, "#edit-key-ttl-select") == ["1 day", "7 days", "30 days"]
