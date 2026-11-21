"""The usage charts take their colour from the active theme's --theme-primary.

The line chart appends '22' to CHART_COLOR for its fill alpha, so a value that
is not a 6-digit hex falls back to the Illinois navy.
"""
import re
from pathlib import Path

import pytest

from tests.test_theme_colors import THEME_HEADS, THEME_PRIMARY

# The chart-colour block in usage.html, from its CHART_COLOR_FALLBACK line to
# the closing "})();" at the start of a line; keep that shape if it is edited.
USAGE = Path(__file__).resolve().parents[2] / "lumen" / "templates" / "usage.html"
SNIPPET = re.search(
    r"^const CHART_COLOR_FALLBACK = .*?^\}\)\(\);$", USAGE.read_text(), re.MULTILINE | re.DOTALL
).group(0)
FALLBACK = "#13294b"

THEMES = [
    pytest.param(THEME_PRIMARY.search(head.read_text()).group(1), id=head.parents[2].name)
    for head in THEME_HEADS
]


def _chart_color(browser, root_style):
    page = browser.new_page()
    try:
        page.set_content(f"<!doctype html><html><head><style>{root_style}</style></head><body></body></html>")
        return page.evaluate(f"() => {{ {SNIPPET}\n return CHART_COLOR; }}")
    finally:
        page.close()


@pytest.mark.parametrize("primary", THEMES)
def test_chart_uses_theme_primary(browser, primary):
    assert _chart_color(browser, f":root {{ --theme-primary: {primary}; }}") == primary


@pytest.mark.parametrize(
    "root_style",
    [
        "",
        ":root { --theme-primary: #123; }",
        ":root { --theme-primary: rgb(0, 30, 98); }",
        ":root { --theme-primary: var(--missing); }",
    ],
    ids=["unset", "short-hex", "rgb", "unresolved"],
)
def test_chart_falls_back_to_navy_without_a_six_digit_hex(browser, root_style):
    assert _chart_color(browser, root_style) == FALLBACK
