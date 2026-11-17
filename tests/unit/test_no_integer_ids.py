"""Guards for the AGENTS.md rule that no integer primary key reaches a user.

Every id a user can see is a row's opaque ``uuid`` (or a public unique key such
as ``model_name``). These tests fail when a route takes an integer id again or a
template renders one.
"""

import re
from pathlib import Path

from werkzeug.routing import IntegerConverter

TEMPLATES = Path(__file__).resolve().parents[2] / "lumen" / "templates"

# A one-line `{{ ... }}` expression, and an id read from an object inside it:
# `.id`, `.<word>_id` or `["id"]` / `['id']`, wherever it appears in the
# expression. A bare variable ending in `_id` (`modal_id`, `input_id`) is a
# DOM id string, not attribute access, and is not flagged.
_EXPRESSION = re.compile(r"\{\{.*?\}\}")
_ID_ACCESS = re.compile(r"\.(?:\w+_)?id\b|\[\s*['\"]id['\"]\s*\]")

# Known-safe expressions, as exact (template, expression) pairs. A new template
# that reads an id in any form fails until it is added here on purpose.
_ALLOWED = {
    # m is a model dict whose id is the public model_name.
    ("connect.html", "{{ m.id }}"),
    # client_id is the free-form OAuth application label, not a key.
    ("oauth_consent.html", "{{ req.client_id }}"),
    ("profile.html", "{{ ('Requested via ' + key.client_id + ((' by ' + key.requested_by) if key.requested_by else '')) | tojson }}"),
    # key.id is only a dict key or set member here; the value rendered is a name or a bool.
    ("profile.html", "{{ key_revokers.get(key.id) | tojson }}"),
    ("project_detail.html", "{{ ((key_creators.get(key.id) or 'Unknown') if show_key_creators else '') | tojson }}"),
    ("project_detail.html", "{{ key_revokers.get(key.id) | tojson }}"),
    ("project_detail.html", "{{ (key.id in rotatable_key_ids) | tojson }}"),
}


def _rendered_ids(line):
    """The `{{ ... }}` expressions on a template line that read an id."""
    return [m.group(0) for m in _EXPRESSION.finditer(line) if _ID_ACCESS.search(m.group(0))]


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
        'data-gid="{{ group.id ~ "" }}"',
        "{{ row.id }}",
        "{{ x.entity_id }}",
        '{{ project["id"] }}',
        "{{ p['id'] }}",
    ):
        assert _rendered_ids(bad), bad
    for ok in (
        "{{ project.uuid }}",
        "{{ modal_id }}",
        "{{ input_id ~ '-label' }}",
        "{{ g.valid }}",
        "{% if p.id %}{% endif %}",
    ):
        assert not _rendered_ids(ok), ok


def test_every_allowed_expression_is_still_in_its_template():
    stale = sorted(
        f"{name}: {expr}" for name, expr in _ALLOWED
        if expr not in (TEMPLATES / name).read_text()
    )
    assert stale == [], "remove stale _ALLOWED entries:\n" + "\n".join(stale)


def test_no_template_renders_an_integer_id():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        name = path.relative_to(TEMPLATES).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for expr in _rendered_ids(line):
                if (name, expr) not in _ALLOWED:
                    offenders.append(f"{name}:{lineno}: {expr}")
    assert offenders == [], "render the row's uuid instead, or allowlist a safe use in _ALLOWED:\n" + "\n".join(offenders)
