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
- SkipTo.js script is present in the page
"""
import pytest
from bs4 import BeautifulSoup


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


def _assert_skip_navigation(soup, url):
    """SkipTo.js (or equivalent skip-nav script) must be loaded."""
    scripts = [s.get("src", "") for s in soup.find_all("script") if s.get("src")]
    inline = [s.get_text() for s in soup.find_all("script") if not s.get("src")]
    has_skipto = any("skipto" in src.lower() for src in scripts)
    assert has_skipto, f"{url}: SkipTo.js not found in page scripts"


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
    _assert_skip_navigation(soup, url)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def group_for_a11y(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = Group(name="a11y-group", config_managed=False, active=True)
        db.session.add(g)
        db.session.commit()
        return {"id": g.id}


# ---------------------------------------------------------------------------
# Page tests
# ---------------------------------------------------------------------------

def test_landing_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    _run_all_checks(resp.data, "/")


def test_models_page(auth_client):
    resp = auth_client.get("/models")
    assert resp.status_code == 200
    _run_all_checks(resp.data, "/models")


def test_admin_groups_page(admin_client):
    resp = admin_client.get("/admin/groups")
    assert resp.status_code == 200
    _run_all_checks(resp.data, "/admin/groups")


def test_admin_group_detail_page(admin_client, group_for_a11y):
    url = f"/admin/groups/{group_for_a11y['id']}"
    resp = admin_client.get(url)
    assert resp.status_code == 200
    _run_all_checks(resp.data, url)


def test_admin_users_page(admin_client):
    resp = admin_client.get("/admin/users")
    assert resp.status_code == 200
    _run_all_checks(resp.data, "/admin/users")


def test_admin_user_limits_page(admin_client, test_user):
    url = f"/admin/users/{test_user['id']}/limits"
    resp = admin_client.get(url)
    assert resp.status_code == 200
    _run_all_checks(resp.data, url)
