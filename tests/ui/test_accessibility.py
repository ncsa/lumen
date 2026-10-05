"""
Structural WCAG 2.1 AA accessibility checks for every server-rendered page.

Rules enforced (from CLAUDE.md):
- <html> has lang attribute
- <main> landmark present
- All <img> have non-empty alt text
- All form controls (<input>/<select>/<textarea>, excluding hidden/button/submit/reset)
  have an associated label via for/id, aria-label, aria-labelledby, or wrapping <label>
- Buttons with no visible text have aria-label
- Bootstrap modals (.modal[role=dialog] or .modal with tabindex) have aria-labelledby
- Data tables (<table> with <thead> and <tbody>) have <caption> or aria-label
- Heading levels do not skip (h1 → h2 → h3, never h1 → h3)
"""
from datetime import datetime
from http import HTTPStatus

from bs4 import BeautifulSoup

from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _soup(html_bytes):
    return BeautifulSoup(html_bytes, "html.parser")


def _assert_lang(soup, url):
    html_tag = soup.find("html")
    assert html_tag and html_tag.get("lang"), f"{url}: <html> missing lang attribute"


def _assert_main_landmark(soup, url):
    assert soup.find("main"), f"{url}: no <main> landmark"


def _assert_images_have_alt(soup, url):
    for img in soup.find_all("img"):
        alt = img.get("alt")
        assert alt is not None, f"{url}: <img src='{img.get('src')}' missing alt"
        assert alt.strip() != "", f"{url}: <img src='{img.get('src')}' has empty alt (use alt='' only for decorative images)"


def _assert_form_controls_labelled(soup, url):
    """Every visible form control must be reachable by a label."""
    label_for_ids = {lbl.get("for") for lbl in soup.find_all("label") if lbl.get("for")}

    skip_types = {"hidden", "submit", "button", "reset", "image"}
    for tag in soup.find_all(["input", "select", "textarea"]):
        if tag.name == "input" and tag.get("type", "text") in skip_types:
            continue
        # Passes if: aria-label, aria-labelledby, id in label_for_ids, or wrapped by <label>
        if tag.get("aria-label") or tag.get("aria-labelledby"):
            continue
        el_id = tag.get("id")
        if el_id and el_id in label_for_ids:
            continue
        if tag.find_parent("label"):
            continue
        assert False, (
            f"{url}: <{tag.name} id='{tag.get('id')}' type='{tag.get('type')}'> "
            f"has no associated label"
        )


def _assert_icon_buttons_labelled(soup, url):
    """Buttons with no visible text must carry aria-label."""
    for btn in soup.find_all("button"):
        # Visible text: strip all child tag text and check for non-empty content
        text = btn.get_text(strip=True)
        if text:
            continue
        aria = btn.get("aria-label") or btn.get("aria-labelledby")
        assert aria, (
            f"{url}: <button class='{btn.get('class')}'> has no visible text and no aria-label"
        )


def _assert_modals_labelled(soup, url):
    """Bootstrap modals (div.modal with tabindex=-1) must have aria-labelledby."""
    for modal in soup.find_all("div", class_="modal"):
        if modal.get("tabindex") == "-1":
            assert modal.get("aria-labelledby"), (
                f"{url}: modal id='{modal.get('id')}' missing aria-labelledby"
            )


def _assert_tables_have_captions(soup, url):
    """Data tables (with both thead and tbody) must have <caption> or aria-label."""
    for table in soup.find_all("table"):
        if not (table.find("thead") and table.find("tbody")):
            continue
        has_caption = table.find("caption") is not None
        has_aria = table.get("aria-label") or table.get("aria-labelledby")
        assert has_caption or has_aria, (
            f"{url}: data table missing <caption> or aria-label"
        )


def _assert_heading_hierarchy(soup, url):
    """Heading levels must not skip (e.g. h1 → h3 without h2 is invalid)."""
    levels = [int(h.name[1]) for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]
    for i in range(1, len(levels)):
        assert levels[i] <= levels[i - 1] + 1, (
            f"{url}: heading jumps from h{levels[i-1]} to h{levels[i]}"
        )




def _assert_rotated_key_modal_accessible(html_bytes, url):
    """The show-once rotated-key modal is labelled, and its key field and copy button have names."""
    soup = _soup(html_bytes)
    modal = soup.find("div", id="rotatedKeyModal")
    assert modal, f"{url}: rotated-key modal missing"
    title_id = modal.get("aria-labelledby")
    assert title_id and modal.find(id=title_id) and modal.find(id=title_id).get_text(strip=True), (
        f"{url}: rotated-key modal title missing"
    )
    assert modal.find("label", attrs={"for": "rotated-key-display"}), f"{url}: rotated key field has no label"
    assert modal.find("button", class_="btn-close").get("aria-label"), f"{url}: close button has no aria-label"
    assert modal.find("button", id="rotated-key-copy-btn").get("aria-label"), f"{url}: copy button has no aria-label"
    status = modal.find(id="rotated-key-copy-status")
    assert status and status.get("role") == "status" and status.get("aria-live") == "polite", (
        f"{url}: copy result is not announced"
    )
    # The rotate confirm uses the shared app dialog; it must be labelled too.
    dialog = soup.find("div", id="app-dialog")
    assert dialog and dialog.get("aria-labelledby") == "app-dialog-title", f"{url}: confirm dialog not labelled"


def _run_all_checks(html_bytes, url):
    soup = _soup(html_bytes)
    _assert_lang(soup, url)
    _assert_main_landmark(soup, url)
    _assert_images_have_alt(soup, url)
    _assert_form_controls_labelled(soup, url)
    _assert_icon_buttons_labelled(soup, url)
    _assert_modals_labelled(soup, url)
    _assert_tables_have_captions(soup, url)
    _assert_heading_hierarchy(soup, url)


# ---------------------------------------------------------------------------
# Page tests
# ---------------------------------------------------------------------------

def test_landing_page(client):
    resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/")


def test_models_page(auth_client):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/models")


def test_models_page_with_access_pills(app, auth_client, test_model):
    with app.app_context():
        model = db.session.get(ModelConfig, test_model["id"])
        model.needs_ack = True
        model.early_access = True
        db.session.commit()
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/models")


def test_models_page_with_granted_pill(app, auth_client, test_user, test_model):
    with app.app_context():
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.add(EntityModelConsent(entity_id=test_user["id"], model_config_id=test_model["id"], consented_at=datetime(2026, 1, 1)))
        db.session.commit()
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/models")


def test_connect_page_logged_out(client):
    resp = client.get("/connect")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/connect")


def test_connect_page_logged_in(auth_client, test_model):
    resp = auth_client.get("/connect")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/connect")


def test_admin_users_page(admin_client):
    resp = admin_client.get("/admin/users")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/admin/users")


def test_admin_config_page_accessibility(admin_client):
    # The config editor's forms are client-side rendered; this checks the
    # server-rendered shell (nav, banners, dialogs) of the page.
    resp = admin_client.get("/admin/config")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/admin/config")


def test_admin_user_profile_page_accessibility(admin_client, test_user):
    url = f"/admin/users/{test_user['id']}/profile"
    resp = admin_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)


def test_own_profile_page_as_admin_accessibility(admin_client):
    # Admin mode renders the Edit User modal on the admin's own profile.
    resp = admin_client.get("/profile")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/profile")
    _assert_rotated_key_modal_accessible(resp.data, "/profile")


def test_project_detail_page_accessibility(app, admin_client):
    with app.app_context():
        from tests.conftest import make_project
        sid = make_project("a11y-svc", initials="AS").id
    url = f"/projects/{sid}"
    resp = admin_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)
    _assert_rotated_key_modal_accessible(resp.data, url)


def test_groups_page_accessibility(auth_client):
    resp = auth_client.get("/groups")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/groups")


def test_groups_page_as_admin_accessibility(admin_client):
    # Admin mode adds the owner search and coin fields to the create dialog.
    resp = admin_client.get("/groups")
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, "/groups")


def test_group_detail_page_accessibility(app, admin_client, test_model, admin_user):
    from tests.conftest import set_model_owner
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.model_group_access import ModelGroupAccess
        group = Group(name="a11y-group", active=True)
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(group_id=group.id, entity_id=admin_user["id"], is_owner=True))
        # A granted model keeps a model row rendered in the Models pane.
        set_model_owner(test_model["id"], admin_user["id"])
        db.session.add(ModelGroupAccess(model_config_id=test_model["id"], group_id=group.id))
        db.session.commit()
        gid = group.id
    url = f"/groups/{gid}"
    resp = admin_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)


# ---------------------------------------------------------------------------
# OAuth key-request pages (device entry, consent, error)
# ---------------------------------------------------------------------------

def _issue_device(client):
    from lumen.extensions import limiter

    limiter.reset()
    return client.post("/oauth/device_authorization", data={
        "client_id": "lumen-cli", "name": "opencode", "author": "alice",
    }).get_json()


def test_device_code_entry_page_accessibility(auth_client):
    url = "/device"
    resp = auth_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)


def test_oauth_error_page_accessibility(client):
    url = "/device/error?reason=unknown_code"
    resp = client.get(url)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    _run_all_checks(resp.data, url)


def test_oauth_consent_page_accessibility(client, auth_client):
    body = _issue_device(client)
    url = f"/device?code={body['user_code']}"
    resp = auth_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)
    # The unverified-application warning must not rely on color alone.
    soup = _soup(resp.data)
    assert "Unverified application" in soup.get_text()


def test_oauth_consent_page_with_existing_key_accessibility(client, auth_client, app, test_user):
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.services.crypto import hash_api_key

    raw = "sk_" + "a" * 40
    with app.app_context():
        db.session.add(APIKey(entity_id=test_user["id"], name="opencode",
                              key_hash=hash_api_key(raw), key_hint="sk_aaaa...aaaa",
                              active=True))
        db.session.commit()
    body = _issue_device(client)
    url = f"/device?code={body['user_code']}"
    resp = auth_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)
    soup = _soup(resp.data)
    cb = soup.find("input", id="overwrite-key")
    assert cb is not None, "overwrite checkbox missing"
    assert soup.find("label", attrs={"for": "overwrite-key"}) is not None
    approve = soup.find("button", id="approve-btn")
    assert approve is not None and approve.get("disabled") is not None


def test_oauth_consent_page_with_ack_model_accessibility(client, auth_client, app):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig

    with app.app_context():
        db.session.add(ModelConfig(
            model_name="a11y-ack-model", input_cost_per_million=1.0,
            output_cost_per_million=1.0, needs_ack=True,
            ack_message="Read me first.",
        ))
        db.session.commit()
    body = _issue_device(client)
    url = f"/device?code={body['user_code']}"
    resp = auth_client.get(url)
    assert resp.status_code == HTTPStatus.OK
    _run_all_checks(resp.data, url)
    soup = _soup(resp.data)
    btn = [b for b in soup.select(".ack-btn") if b.get("data-name") == "a11y-ack-model"]
    assert btn and btn[0].get("aria-label") == "Acknowledge a11y-ack-model"
    table = soup.find("table", id="consent-models")
    assert table is not None and table.find("caption") is not None
    assert "required" in table.get_text(), "state must not rely on color alone"
