"""Theme colours: the stylesheet and templates follow the active theme's primary colour.

Each theme's ``head_extras.html`` sets ``--theme-primary``; ``app.css`` and the
templates read it through ``--lumen-primary`` and keep the Illinois navy only as
a fallback.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_CSS = ROOT / "lumen" / "static" / "css" / "app.css"
TEMPLATES_DIR = ROOT / "lumen" / "templates"
TEMPLATES = sorted(TEMPLATES_DIR.glob("**/*.html"))
THEME_HEADS = sorted(ROOT.glob("themes/*/templates/theme/head_extras.html"))

# Illinois navy, its darker hover shade and its translucent focus-ring form.
NAVY = re.compile(r"#13294b\b|#0e1f38\b|rgba\(\s*19\s*,\s*41\s*,\s*75\s*,", re.IGNORECASE)
# The only places the navy may still appear in app.css: the single
# --lumen-primary fallback, and the existing var(--bubble-*, ...) fallbacks.
ALLOWED = [
    re.compile(r"--lumen-primary\s*:\s*var\(\s*--theme-primary\s*,\s*#13294b\s*\)", re.IGNORECASE),
    re.compile(r"var\(\s*--bubble-[\w-]+\s*,[^)]*\)", re.IGNORECASE),
]
# The only place it may appear in a template: the usage chart colour's fallback
# when --theme-primary is not a 6-digit hex (the line chart appends '22' for its
# fill alpha), as its own declaration line in usage.html.
TEMPLATE_ALLOWED = {
    "usage.html": [re.compile(r"^const CHART_COLOR_FALLBACK = '#13294b';$", re.MULTILINE)],
}
THEME_PRIMARY = re.compile(r"--theme-primary\s*:\s*(#[0-9a-fA-F]{6})\b")


def _hardcoded_navy(text, allowed=ALLOWED):
    for pattern in allowed:
        text = pattern.sub("", text)
    return [
        f"line {text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}"
        for m in NAVY.finditer(text)
    ]


def _luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_app_css_has_no_hardcoded_navy():
    assert _hardcoded_navy(APP_CSS.read_text()) == []


def test_app_css_keeps_the_navy_fallback():
    # Guards the scan above: without the fallback the Illinois look would break
    # wherever head_extras.html is not loaded.
    assert ALLOWED[0].search(APP_CSS.read_text())


def _template_navy(name, text):
    return _hardcoded_navy(text, TEMPLATE_ALLOWED.get(name, []))


def test_templates_have_no_hardcoded_navy():
    assert TEMPLATES, "no templates found"
    found = {
        name: hits
        for t in TEMPLATES
        if (hits := _template_navy(name := t.relative_to(TEMPLATES_DIR).as_posix(), t.read_text()))
    }
    assert found == {}


def test_template_scan_allows_the_chart_fallback_only_in_usage_html():
    fallback = "const CHART_COLOR_FALLBACK = '#13294b';\n"
    assert _template_navy("usage.html", fallback) == []
    assert _template_navy("other.html", fallback) == ["line 1: #13294b"]
    assert _template_navy("admin/usage.html", fallback) == ["line 1: #13294b"]
    # Only that declaration line: the same constant elsewhere in usage.html,
    # and the app.css allowances, are still flagged.
    assert _template_navy("usage.html", "body { color: CHART_COLOR_FALLBACK = '#13294b'; }\n")
    assert _template_navy("usage.html", "const CHART_COLOR = '#13294b';\n")
    assert _template_navy("usage.html", ".a { background: var(--bubble-user, #13294b); }\n")
    assert _template_navy("usage.html", ":root { --lumen-primary: var(--theme-primary, #13294b); }\n")


def test_usage_chart_reads_theme_primary():
    usage = (ROOT / "lumen" / "templates" / "usage.html").read_text()
    assert "getPropertyValue('--theme-primary')" in usage
    assert "CHART_COLOR_FALLBACK = '#13294b'" in usage


def test_scan_flags_hardcoded_navy():
    css = (
        ":root { --lumen-primary: var(--theme-primary, #13294b); }\n"
        ".a { background: var(--bubble-user, #13294B); }\n"
        ".b { color: #13294B; }\n"
        ".c { color: #0e1f38; }\n"
        ".d { box-shadow: 0 0 0 1px rgba(19, 41, 75, 0.4); }\n"
    )
    found = _hardcoded_navy(css)
    assert [f.split(":")[0] for f in found] == ["line 3", "line 4", "line 5"]


def test_every_theme_is_checked():
    names = {p.parents[2].name for p in THEME_HEADS}
    assert {"default", "illinois", "uic", "uis"} <= names


@pytest.mark.parametrize("head", THEME_HEADS, ids=lambda p: p.parents[2].name)
def test_theme_defines_primary(head):
    assert THEME_PRIMARY.search(head.read_text()), f"{head} does not set --theme-primary"


@pytest.mark.parametrize("head", THEME_HEADS, ids=lambda p: p.parents[2].name)
def test_white_text_on_theme_primary_meets_wcag_aa(head):
    match = THEME_PRIMARY.search(head.read_text())
    assert match, f"{head} does not set --theme-primary"
    assert _contrast("#ffffff", match.group(1)) >= 4.5
