"""``docs/swagger.yaml`` keeps up with the official ``/v1/`` API.

Every ``/v1`` route Lumen registers needs an operation in the spec, with the
access classes the code actually enforces in ``x-access``. A new, changed or
removed ``/v1`` route fails here until the spec is updated (see AGENTS.md).
"""
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

from lumen.blueprints.api.routes import MONITOR_ENDPOINTS

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC_PATH = ROOT / "docs" / "swagger.yaml"

ACCESS_CLASSES = {"public", "monitor", "normal", "admin"}
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
# Werkzeug's "<converter:name>" or "<name>" becomes OpenAPI's "{name}".
_RULE_ARG_RE = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")
# How each access class is named in the "Access: ..." first paragraph of an
# operation's description; Swagger UI hides x- fields, so readers see only these.
ACCESS_WORDS = {
    "no key": "public",
    "normal key": "normal",
    "monitoring key": "monitor",
    "admin key": "admin",
}


@pytest.fixture(scope="module")
def spec():
    with SPEC_PATH.open() as f:
        return yaml.safe_load(f)


def _spec_operations(spec):
    """``{(METHOD, path): operation}`` for every operation in the spec."""
    return {
        (method.upper(), path): op
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in HTTP_METHODS
    }


def _route_endpoints(app):
    """``{(METHOD, path): endpoint}`` for every ``/v1`` route, HEAD and OPTIONS aside."""
    routes = {}
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/v1/"):
            continue
        path = _RULE_ARG_RE.sub(r"{\1}", rule.rule)
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            routes[(method, path)] = rule.endpoint
    return routes


def _expected_access(endpoint):
    """The access classes the code enforces for ``endpoint``."""
    if endpoint in MONITOR_ENDPOINTS:
        return {"normal", "monitor", "admin"}
    if endpoint.startswith("admin_api."):
        return {"admin"}
    return {"normal"}


def test_spec_is_valid_openapi(spec):
    validate(spec)


def test_spec_lists_exactly_the_v1_routes(app, spec):
    routes = set(_route_endpoints(app))
    documented = set(_spec_operations(spec))
    assert routes - documented == set(), "add these /v1 routes to docs/swagger.yaml"
    assert documented - routes == set(), "remove these operations from docs/swagger.yaml"


def test_every_operation_has_valid_access_classes(spec):
    for key, op in _spec_operations(spec).items():
        access = op.get("x-access")
        assert access, f"{key} has no x-access"
        assert set(access) <= ACCESS_CLASSES, f"{key} has unknown x-access {access}"
        assert len(access) == len(set(access)), f"{key} repeats an x-access class"


def _described_access(description):
    """The access classes named in the description's first paragraph, or None without one."""
    first = " ".join(description.split("\n\n", 1)[0].split())
    if not first.startswith("Access: "):
        return None
    return {cls for words, cls in ACCESS_WORDS.items() if words in first}


def test_descriptions_state_the_same_access_as_x_access(spec):
    for key, op in _spec_operations(spec).items():
        described = _described_access(op.get("description", ""))
        assert described is not None, f"{key} description does not start with 'Access: '"
        assert described == set(op["x-access"]), f"{key} description names {described}, x-access is {op['x-access']}"


def test_access_classes_match_the_code(app, spec):
    operations = _spec_operations(spec)
    for key, endpoint in _route_endpoints(app).items():
        assert set(operations[key]["x-access"]) == _expected_access(endpoint), key


def test_version_matches_pyproject(spec):
    with (ROOT / "pyproject.toml").open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert spec["info"]["version"] == version
