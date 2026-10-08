"""Footer rendering for the Illinois theme and the default theme."""
import re
from contextlib import contextmanager
from http import HTTPStatus

import pytest

ILLINOIS_ACTION_URLS = [
    "https://chat.illinois.edu",
    "https://lumen.computes.illinois.edu",
    "https://llmhub.computes.illinois.edu",
    "https://llmflux.ncsa.ai/",
]


@contextmanager
def _theme(app, name):
    old_theme, old_name = app.config["THEME"], app.config["THEME_NAME"]
    app.config["THEME"] = app.config["THEME_CACHE"][name]
    app.config["THEME_NAME"] = name
    try:
        yield
    finally:
        app.config["THEME"], app.config["THEME_NAME"] = old_theme, old_name


def _actions_block(html):
    match = re.search(r'<div slot="actions">(.*?)</div>', html, re.S)
    assert match, "footer actions slot not rendered"
    return match.group(1)


@pytest.fixture
def illinois_footer(app, client):
    with _theme(app, "illinois"):
        resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    return resp.get_data(as_text=True)


def test_illinois_footer_actions_in_order(illinois_footer):
    actions = _actions_block(illinois_footer)
    assert re.findall(r'href="([^"]+)"', actions) == ILLINOIS_ACTION_URLS


def test_illinois_footer_menus(app, illinois_footer):
    headings = re.findall(
        r'<nav class="ilw-footer-menu" aria-labelledby="([^"]+)">\s*<h2 id="([^"]+)">([^<]+)</h2>',
        illinois_footer,
    )
    assert [title for _, _, title in headings] == ["Illinois Computes", "Lumen"]
    ids = [label for label, _, _ in headings]
    assert ids == [h2_id for _, h2_id, _ in headings]
    assert len(set(ids)) == len(ids)

    github_url = app.config.get("GITHUB_URL", "")
    lumen_menu = illinois_footer.split('aria-labelledby="footer-menu-2"', 1)[1].split("</nav>", 1)[0]
    assert f'href="{github_url}"' in lumen_menu
    assert f'href="{github_url}/issues"' in lumen_menu
    actions = _actions_block(illinois_footer)
    assert "GitHub Repository" not in actions
    assert "Request Feature" not in actions


def test_default_footer_still_renders(app, client):
    with _theme(app, "default"):
        resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    html = resp.get_data(as_text=True)
    assert "GitHub Repository" in html
    assert "ilw-footer-menu" not in html
