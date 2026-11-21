"""Primary button states in every theme, with and without CSS color-mix().

app.css derives the hover and focus-ring colours from --theme-primary with
color-mix() inside @supports, and falls back to the plain primary colour. A
browser without color-mix() is simulated by renaming the function in the
served stylesheet, which makes every @supports (color: color-mix(...)) test
false, as it is in such a browser.
"""
import re

import pytest

from tests.test_theme_colors import APP_CSS, THEME_HEADS, THEME_PRIMARY, _contrast

CSS = APP_CSS.read_text()
NO_COLOR_MIX_CSS = CSS.replace("color-mix(", "color-mix-unsupported(")
PAGE = """<!doctype html>
<html><head>
<style>:root {{ --theme-primary: {primary}; }} body {{ background: #fff; }}</style>
<style>{css}</style>
</head><body>
<button class="btn btn-primary" id="primary">Send</button>
<button class="btn btn-outline-primary" id="outline">Cancel</button>
<a class="btn btn-login" id="login" href="#">Login with OAuth2</a>
</body></html>"""
RGBA = re.compile(r"rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?\)")
# Chromium reports color-mix() results in this form, with 0..1 channels.
SRGB = re.compile(r"color\(srgb ([\d.e-]+) ([\d.e-]+) ([\d.e-]+)(?: / ([\d.]+))?\)")


THEMES = [
    pytest.param(THEME_PRIMARY.search(head.read_text()).group(1), id=head.parents[2].name)
    for head in THEME_HEADS
]


def _colours(value):
    """(hex, alpha) for every colour in a computed CSS value, in order."""
    found = [(m.start(), [int(c) for c in m.groups()[:3]], m.group(4))
             for m in RGBA.finditer(value)]
    found += [(m.start(), [round(float(c) * 255) for c in m.groups()[:3]], m.group(4))
              for m in SRGB.finditer(value)]
    return [
        ("#{:02x}{:02x}{:02x}".format(*rgb), float(alpha) if alpha else 1.0)
        for _, rgb, alpha in sorted(found)
    ]


def _style(page, selector, prop):
    return page.eval_on_selector(selector, f"e => getComputedStyle(e).{prop}")


@pytest.fixture
def open_page(browser):
    pages = []

    def _open(primary, css):
        page = browser.new_page()
        pages.append(page)
        page.set_content(PAGE.format(primary=primary, css=css))
        return page

    yield _open
    for page in pages:
        page.close()


def test_without_color_mix_the_derived_colours_fall_back_to_primary(open_page):
    page = open_page("#001e62", NO_COLOR_MIX_CSS)
    for name in ("hover", "ring", "ring-soft"):
        value = page.evaluate(
            "getComputedStyle(document.documentElement)"
            f".getPropertyValue('--lumen-primary-{name}')"
        )
        assert value.strip().lower() == "#001e62", name


@pytest.mark.parametrize("css", [CSS, NO_COLOR_MIX_CSS], ids=["color-mix", "no-color-mix"])
@pytest.mark.parametrize("primary", THEMES)
def test_hovered_buttons_keep_a_readable_background(open_page, primary, css):
    page = open_page(primary, css)
    for selector in ("#primary", "#outline", "#login"):
        page.hover(selector)
        [(bg, alpha)] = _colours(_style(page, selector, "backgroundColor"))
        [(fg, _)] = _colours(_style(page, selector, "color"))
        assert alpha == 1.0, f"{selector} hover background is see-through"
        assert fg == "#ffffff"
        assert _contrast(fg, bg) >= 4.5, f"{selector} hover: white on {bg}"


@pytest.mark.parametrize("css", [CSS, NO_COLOR_MIX_CSS], ids=["color-mix", "no-color-mix"])
@pytest.mark.parametrize("primary", THEMES)
def test_keyboard_focus_ring_is_visible(open_page, primary, css):
    page = open_page(primary, css)
    for selector in ("#primary", "#outline", "#login"):
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == selector[1:]
        shadow = _style(page, selector, "boxShadow")
        ring = [(c, a) for c, a in _colours(shadow) if c != "#ffffff" and a > 0]
        assert ring, f"{selector} has no visible focus ring: {shadow}"
        # The ring must differ from the button's own background, or it merges
        # into the button instead of marking it.
        [(bg, _)] = _colours(_style(page, selector, "backgroundColor"))
        # A white gap or a see-through ring also sets it apart.
        assert "rgb(255, 255, 255)" in shadow or any(c != bg or a < 1 for c, a in ring)


def test_illinois_with_color_mix_keeps_its_former_colours(open_page):
    page = open_page("#13294B", CSS)
    page.hover("#primary")
    assert _colours(_style(page, "#primary", "backgroundColor")) == [("#0e1f38", 1.0)]
    page.mouse.move(0, 400)
    page.keyboard.press("Tab")
    [(ring, alpha)] = _colours(_style(page, "#primary", "boxShadow"))
    assert ring == "#13294b" and alpha == pytest.approx(0.4, abs=0.01)
