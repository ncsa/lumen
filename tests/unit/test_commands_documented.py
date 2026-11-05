"""The Admin Guide's Operator Commands page keeps up with the flask commands.

Every command Lumen registers on ``app.cli`` needs its own section on
``docs/admin/commands.md``, and the ``upgrade`` section names every aggregate that
``flask upgrade`` backfills. A new command or a new usage aggregate fails here until
the page is updated (see AGENTS.md).
"""
import re
from pathlib import Path

import pytest

from lumen.commands import USAGE_AGGREGATES

COMMANDS_PAGE = Path(__file__).resolve().parent.parent.parent / "docs" / "admin" / "commands.md"

_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.MULTILINE)


def _headings(text):
    """Return ``(level, title, start, end)`` for every heading, ``end`` being the
    offset where the section (up to the next heading of the same or a higher level)
    stops."""
    found = [(len(m.group(1)), m.group(2), m.start(), m.end()) for m in _HEADING_RE.finditer(text)]
    sections = []
    for i, (level, title, start, body_start) in enumerate(found):
        end = next((s for lvl, _, s, _ in found[i + 1:] if lvl <= level), len(text))
        sections.append((level, title, body_start, end))
    return sections


def _section(text, title):
    for _, heading, start, end in _headings(text):
        if heading == title:
            return text[start:end]
    return None


@pytest.fixture(scope="module")
def page():
    return COMMANDS_PAGE.read_text(encoding="utf-8")


def _registered_commands(app):
    """Names of the commands on ``app.cli`` that Lumen defines.

    Flask-Migrate's ``db`` group is on ``app.cli`` too; it is a library's command,
    mentioned on the page but not given a section of its own.
    """
    return sorted(name for name, cmd in app.cli.commands.items()
                  if cmd.callback is not None and cmd.callback.__module__.startswith("lumen."))


def test_every_registered_command_has_a_heading(app, page):
    commands = _registered_commands(app)
    assert commands, "app.cli has no commands; the check would pass vacuously"
    titles = {title for _, title, _, _ in _headings(page)}
    missing = [name for name in commands if f"`{name}`" not in titles]
    assert not missing, (
        f"docs/admin/commands.md has no heading for: {', '.join(missing)}. "
        "Add a section titled with the command name in backticks."
    )


def test_upgrade_section_names_every_usage_aggregate(page):
    section = _section(page, "`upgrade`")
    assert section is not None, "docs/admin/commands.md has no `upgrade` section"
    missing = [name for name in USAGE_AGGREGATES if f"`{name}`" not in section]
    assert not missing, (
        f"The `upgrade` section of docs/admin/commands.md does not name: {', '.join(missing)}. "
        "flask upgrade backfills every USAGE_AGGREGATES entry; list it there."
    )


def test_upgrade_section_lists_usage_aggregates_in_refresh_order(page):
    section = _section(page, "`upgrade`")
    positions = [section.find(f"`{name}`") for name in USAGE_AGGREGATES]
    assert positions == sorted(positions), (
        "The `upgrade` section must list the usage aggregates in the order flask upgrade "
        f"refreshes them (USAGE_AGGREGATES): {', '.join(USAGE_AGGREGATES)}"
    )
