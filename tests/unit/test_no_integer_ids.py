"""Guards for the AGENTS.md rule that no integer primary key reaches a user.

Every id a user can see is a row's opaque ``uuid`` (or a public unique key such
as ``model_name``). These tests fail when a route takes an integer id again or a
template renders one.
"""

import re
from pathlib import Path

from werkzeug.routing import IntegerConverter

TEMPLATES = Path(__file__).resolve().parents[2] / "lumen" / "templates"

# Template variable names that hold a user, project, group, API key,
# conversation or auth request row (or a dict built from one).
_ROW_NAMES = (
    "user", "u", "project", "proj", "p", "entity", "e", "owner", "member",
    "group", "grp", "g", "key", "api_key", "k",
    "conversation", "conv", "c", "req", "auth_request",
)
_NAMES = "|".join(_ROW_NAMES)
# The whole expression is `<obj>.id` (optionally piped through filters), or an
# argument passes it on, e.g. url_for(..., sid=project.id).
_RENDERED_ID = re.compile(
    r"\{\{-?\s*(?:" + _NAMES + r")\.id\s*(?:\|[^}]*)?-?\}\}"
    r"|\{\{[^}]*=\s*(?:" + _NAMES + r")\.id\b[^}]*\}\}"
)


def test_no_route_uses_the_int_converter(app):
    offenders = sorted(
        f"{rule.rule} ({rule.endpoint})"
        for rule in app.url_map.iter_rules()
        if any(isinstance(conv, IntegerConverter) for conv in rule._converters.values())
    )
    assert offenders == [], "routes must take ids with <uuid:...>, not <int:...>: " + ", ".join(offenders)


def test_guard_pattern_catches_rendered_ids():
    for bad in (
        "{{ project.id }}",
        "{{ key.id|tojson }}",
        "{{- req.id -}}",
        "{{ url_for('projects.detail', sid=p.id) }}",
        "{{ conversation.id | string }}",
    ):
        assert _RENDERED_ID.search(bad), bad
    for ok in (
        "{{ project.uuid }}",
        "{{ key_revokers.get(key.id) | tojson }}",
        "{{ (key.id in rotatable_key_ids) | tojson }}",
        "{{ m.id }}",  # connect.html: a model dict whose id is model_name
    ):
        assert not _RENDERED_ID.search(ok), ok


def test_no_template_renders_an_integer_id():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for match in _RENDERED_ID.finditer(line):
                offenders.append(f"{path.relative_to(TEMPLATES)}:{lineno}: {match.group(0)}")
    assert offenders == [], "render the row's uuid instead:\n" + "\n".join(offenders)
