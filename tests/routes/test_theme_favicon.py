"""Per-theme favicon <link> tags rendered from the theme's `favicon:` block."""
import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from http import HTTPStatus

import pytest

ILLINOIS_CDN = "https://cdn.brand.illinois.edu/icons"

# (rel, href) pairs each theme must render, in order.
EXPECTED_LINKS = {
    "default": [
        ("icon", "/theme-static/favicon/favicon.ico"),
        ("icon", "/theme-static/favicon/icon.svg"),
        ("apple-touch-icon", "/theme-static/favicon/apple-touch-icon.png"),
    ],
    "illinois": [
        ("icon", f"{ILLINOIS_CDN}/favicon.ico"),
        ("icon", f"{ILLINOIS_CDN}/icon.svg"),
        ("apple-touch-icon", f"{ILLINOIS_CDN}/touch-icon.png"),
    ],
    "uic": [
        ("icon", "/theme-static/favicon/favicon.ico"),
        ("apple-touch-icon", "/theme-static/favicon/apple-touch-icon.png"),
    ],
    "uis": [
        ("icon", "/theme-static/favicon/favicon.ico"),
    ],
}

_LINK_RE = re.compile(r'<link rel="(icon|apple-touch-icon)" href="([^"]+)"')


@contextmanager
def _theme(app, name):
    old_theme, old_name = app.config["THEME"], app.config["THEME_NAME"]
    app.config["THEME"] = app.config["THEME_CACHE"][name]
    app.config["THEME_NAME"] = name
    try:
        yield
    finally:
        app.config["THEME"], app.config["THEME_NAME"] = old_theme, old_name


def _favicon_links(html):
    return _LINK_RE.findall(html)


@pytest.mark.parametrize("theme", sorted(EXPECTED_LINKS))
@pytest.mark.parametrize("path,status", [("/", HTTPStatus.OK), ("/does-not-exist", HTTPStatus.NOT_FOUND)])
def test_anonymous_pages_render_favicon_links(app, client, theme, path, status):
    with _theme(app, theme):
        resp = client.get(path)
    assert resp.status_code == status
    assert _favicon_links(resp.get_data(as_text=True)) == EXPECTED_LINKS[theme]


@pytest.mark.parametrize("theme", sorted(EXPECTED_LINKS))
def test_authenticated_page_renders_favicon_links(app, auth_client, theme):
    with _theme(app, theme):
        resp = auth_client.get("/chat")
    assert resp.status_code == HTTPStatus.OK
    assert _favicon_links(resp.get_data(as_text=True)) == EXPECTED_LINKS[theme]


@pytest.mark.parametrize("theme", sorted(EXPECTED_LINKS))
def test_local_favicon_files_are_served_as_images(app, client, theme):
    local = [href for _, href in EXPECTED_LINKS[theme] if href.startswith("/")]
    with _theme(app, theme):
        for href in local:
            resp = client.get(href)
            assert resp.status_code == HTTPStatus.OK, href
            assert resp.mimetype.startswith("image/"), (href, resp.mimetype)


def test_default_svg_has_single_svg_root(app, client):
    with _theme(app, "default"):
        resp = client.get("/theme-static/favicon/icon.svg")
    assert resp.status_code == HTTPStatus.OK
    assert resp.mimetype == "image/svg+xml"
    root = ET.fromstring(resp.data)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert not root.findall(".//{http://www.w3.org/2000/svg}svg")


def test_protocol_relative_values_are_used_as_is(app, client):
    cache = app.config["THEME_CACHE"]
    original = cache["default"]
    cache["default"] = {**original, "favicon": {
        "ico": "//cdn.example.org/favicon.ico",
        "apple_touch": "//cdn.example.org/touch.png",
    }}
    try:
        with _theme(app, "default"):
            html = client.get("/").get_data(as_text=True)
    finally:
        cache["default"] = original
    assert _favicon_links(html) == [
        ("icon", "//cdn.example.org/favicon.ico"),
        ("apple-touch-icon", "//cdn.example.org/touch.png"),
    ]


def test_illinois_links_are_brand_cdn(app, client):
    with _theme(app, "illinois"):
        html = client.get("/").get_data(as_text=True)
    assert [href for _, href in _favicon_links(html)] == [
        f"{ILLINOIS_CDN}/favicon.ico",
        f"{ILLINOIS_CDN}/icon.svg",
        f"{ILLINOIS_CDN}/touch-icon.png",
    ]


@pytest.fixture
def no_favicon_theme(app):
    """A copy of the default theme without its `favicon:` block, injected into THEME_CACHE.

    It keeps the default theme's directory name so theme templates still resolve.
    """
    cache = app.config["THEME_CACHE"]
    original = cache["default"]
    cache["default"] = {k: v for k, v in original.items() if k != "favicon"}
    try:
        with _theme(app, "default"):
            yield
    finally:
        cache["default"] = original


def _assert_no_favicon_links(resp, status):
    assert resp.status_code == status
    html = resp.get_data(as_text=True)
    assert 'rel="icon"' not in html
    assert 'rel="apple-touch-icon"' not in html


def test_theme_without_favicon_block_anonymous_pages(client, no_favicon_theme):
    _assert_no_favicon_links(client.get("/"), HTTPStatus.OK)
    _assert_no_favicon_links(client.get("/does-not-exist"), HTTPStatus.NOT_FOUND)


def test_theme_without_favicon_block_authenticated_page(auth_client, no_favicon_theme):
    _assert_no_favicon_links(auth_client.get("/chat"), HTTPStatus.OK)


# ---------------------------------------------------------------------------
# /favicon.ico route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("theme", ["default", "uic", "uis"])
def test_favicon_ico_serves_local_theme_file(app, client, theme):
    with _theme(app, theme):
        resp = client.get("/favicon.ico")
    assert resp.status_code == HTTPStatus.OK
    assert resp.mimetype.startswith("image/"), resp.mimetype


def test_favicon_ico_redirects_to_absolute_url(app, client):
    with _theme(app, "illinois"):
        resp = client.get("/favicon.ico")
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["Location"] == f"{ILLINOIS_CDN}/favicon.ico"


def test_favicon_ico_redirects_protocol_relative_url(app, client):
    cache = app.config["THEME_CACHE"]
    original = cache["default"]
    cache["default"] = {**original, "favicon": {"ico": "//cdn.example.org/favicon.ico"}}
    try:
        with _theme(app, "default"):
            resp = client.get("/favicon.ico")
    finally:
        cache["default"] = original
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["Location"] == "//cdn.example.org/favicon.ico"


def test_favicon_ico_404_without_favicon_block(client, no_favicon_theme):
    assert client.get("/favicon.ico").status_code == HTTPStatus.NOT_FOUND


def test_favicon_ico_follows_email_theme_override(app, client):
    """`g.theme` decides, so an `email_themes` override picks the user's theme."""
    old = app.config.get("EMAIL_THEMES", {})
    app.config["EMAIL_THEMES"] = {"@illinois.edu": "illinois"}
    try:
        with client.session_transaction() as sess:
            sess["entity_email"] = "someone@illinois.edu"
        with _theme(app, "default"):
            resp = client.get("/favicon.ico")
    finally:
        app.config["EMAIL_THEMES"] = old
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["Location"] == f"{ILLINOIS_CDN}/favicon.ico"


def test_favicon_ico_needs_no_login(app, client):
    """Anonymous fetches are answered directly, never sent to the login page."""
    with _theme(app, "default"):
        resp = client.get("/favicon.ico")
    assert resp.status_code == HTTPStatus.OK
    assert "Location" not in resp.headers


def test_favicon_ico_is_exempt_from_rate_limits(app, client):
    """With a 1-per-minute default limit active, /favicon.ico still always answers.

    The theme-static control request proves the injected limit is really enforced.
    """
    from flask_limiter import Limit
    from flask_limiter.util import get_remote_address

    from lumen.extensions import limiter

    manager = limiter.limit_manager
    original = manager._default_limits
    manager.set_default_limits([
        Limit(limit_provider="1 per minute", key_function=get_remote_address,
              finalized=False).bind(limiter),
    ])
    try:
        with app.app_context():
            limiter.reset()
        with _theme(app, "default"):
            control = [client.get("/theme-static/favicon/favicon.ico").status_code for _ in range(2)]
            favicon = [client.get("/favicon.ico").status_code for _ in range(5)]
    finally:
        manager.set_default_limits(original)
        with app.app_context():
            limiter.reset()
    assert control == [HTTPStatus.OK, HTTPStatus.TOO_MANY_REQUESTS]
    assert favicon == [HTTPStatus.OK] * 5
