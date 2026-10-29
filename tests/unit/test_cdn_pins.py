"""Every CDN <script>/<link> tag is pinned to an exact version and loaded with SRI.

A floating URL (``npm/marked/...``, ``chart.js@4``) lets an upstream release
change the UI without a code change, and a tag without ``integrity`` lets a
tampered CDN file run. This scans the app, theme and monitor templates offline;
it checks that the attributes are present and well formed, not that a hash is
correct (the browser enforces that, and the key-tables UI test does for the
Bootstrap bundle).
"""
import ast
import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIRS = [
    ROOT / "lumen" / "templates",
    ROOT / "themes",
    ROOT / "scripts" / "monitor" / "monitor" / "templates",
]
CDN_HOSTS = ("https://cdn.jsdelivr.net/", "https://cdnjs.cloudflare.com/")
# The version must sit where each CDN resolves it, not anywhere in the path:
# jsDelivr npm/<pkg>@1.2.3/... (pkg may be scoped, @scope/pkg), cdnjs ajax/libs/<lib>/1.2.3/...
EXACT_VERSION = re.compile(
    r"https://cdn\.jsdelivr\.net/npm/(?:@[^/@?#]+/)?[^/@?#]+@\d+\.\d+\.\d+/"
    r"|https://cdnjs\.cloudflare\.com/ajax/libs/[^/?#]+/\d+\.\d+\.\d+/"
)
# integrity is a space-separated list; each entry must be well formed and one
# must be sha384 (48 bytes, so exactly 64 base64 characters and no padding).
SRI_ENTRY = re.compile(r"sha256-[A-Za-z0-9+/]{43}=|sha384-[A-Za-z0-9+/]{64}|sha512-[A-Za-z0-9+/]{86}==")
REGENERATE = "curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A"
BASE_HTML = ROOT / "lumen" / "templates" / "base.html"
KEY_TABLES_TEST = ROOT / "tests" / "ui" / "test_key_tables.py"
GOOD_SRI = "sha384-" + "A" * 64


class _CdnTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("script", "link"):
            return
        attrs = dict(attrs)
        url = attrs.get("src") if tag == "script" else attrs.get("href")
        if url and url.startswith(CDN_HOSTS):
            self.tags.append((self.getpos()[0], url, attrs))


def _parse(path):
    parser = _CdnTags()
    parser.feed(path.read_text())
    return parser.tags


def _cdn_tags():
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.rglob("*.html")):
            for lineno, url, attrs in _parse(path):
                yield f"{path.relative_to(ROOT)}:{lineno}", url, attrs


def _tag_problems(url, attrs):
    problems = []
    if not EXACT_VERSION.match(url):
        problems.append("is not pinned to an exact x.y.z package version")
    entries = (attrs.get("integrity") or "").split()
    if not entries or not all(SRI_ENTRY.fullmatch(e) for e in entries) or not any(e.startswith("sha384-") for e in entries):
        problems.append('lacks a well-formed integrity="sha384-..."')
    if attrs.get("crossorigin") != "anonymous":
        problems.append('lacks crossorigin="anonymous"')
    return problems


def _assignment(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    return None


@pytest.mark.parametrize("directory", TEMPLATE_DIRS, ids=lambda d: str(d.relative_to(ROOT)))
def test_template_dir_exists(directory):
    # rglob on a missing directory yields nothing, which would silently drop its tags from the check.
    assert directory.is_dir(), f"{directory} is gone; update TEMPLATE_DIRS"


def test_cdn_tags_are_found():
    assert any(True for _ in _cdn_tags()), "no CDN tags found; did the template paths move?"


def test_cdn_tags_are_pinned_and_hashed():
    problems = [f"{where}: {url} {p}" for where, url, attrs in _cdn_tags() for p in _tag_problems(url, attrs)]
    assert not problems, (
        "CDN tags must pin an exact version and carry SRI. Generate a hash with\n"
        f"  {REGENERATE}\n"
        "and cross-check it against a second source (the jsDelivr data API or the npm tarball).\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize("url", [
    "https://cdn.jsdelivr.net/npm/marked@18.1.2/lib/marked.umd.js",
    "https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js",
    "https://cdn.jsdelivr.net/npm/@scope/pkg@1.2.3/dist/pkg.js",
    "https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.19.1/katex.min.js",
])
def test_exact_package_version_passes(url):
    assert _tag_problems(url, {"integrity": GOOD_SRI, "crossorigin": "anonymous"}) == []


@pytest.mark.parametrize("url", [
    "https://cdn.jsdelivr.net/npm/marked/marked.min.js",
    "https://cdn.jsdelivr.net/npm/chart.js@4",
    "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js",
    "https://cdn.jsdelivr.net/npm/marked@latest/lib/marked.umd.js",
    "https://cdn.jsdelivr.net/npm/marked@18.1.2",
    # A version-shaped segment elsewhere in the path or query is not a pin.
    "https://cdn.jsdelivr.net/npm/foo/dist/1.2.3/foo.js",
    "https://cdn.jsdelivr.net/npm/marked@18/lib/1.2.3/marked.umd.js",
    "https://cdn.jsdelivr.net/npm/foo/foo.js?v=1.2.3/",
    "https://cdn.jsdelivr.net/npm/@scope/pkg/1.2.3/pkg.js",
    "https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.19/katex.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/KaTeX/latest/dist/1.2.3/katex.min.js",
    "https://cdnjs.cloudflare.com/foo/1.2.3/katex.min.js",
])
def test_unpinned_package_version_fails(url):
    problems = _tag_problems(url, {"integrity": GOOD_SRI, "crossorigin": "anonymous"})
    assert problems == ["is not pinned to an exact x.y.z package version"]


@pytest.mark.parametrize("integrity, ok", [
    (GOOD_SRI, True),
    (GOOD_SRI + " sha512-" + "B" * 86 + "==", True),
    ("sha512-" + "B" * 86 + "==", False),  # no sha384
    ("sha384-" + "A" * 63, False),
    (GOOD_SRI + " md5-abc", False),
    ("", False),
])
def test_integrity_format(integrity, ok):
    url = "https://cdn.jsdelivr.net/npm/marked@18.1.2/lib/marked.umd.js"
    problems = _tag_problems(url, {"integrity": integrity, "crossorigin": "anonymous"})
    assert (problems == []) is ok


def test_key_tables_bootstrap_matches_base_html():
    source = KEY_TABLES_TEST.read_text()
    node = _assignment(source, "BOOTSTRAP_JS_URL")
    assert isinstance(node, ast.Constant), f"BOOTSTRAP_JS_URL is no longer a string constant in {KEY_TABLES_TEST}"
    url = node.value
    bundles = [(u, a) for _, u, a in _parse(BASE_HTML) if "bootstrap.bundle" in u]
    assert [u for u, _ in bundles] == [url], (
        f"BOOTSTRAP_JS_URL in tests/ui/test_key_tables.py ({url}) must match the bundle "
        f"base.html loads ({[u for u, _ in bundles]}); also replace tests/fixtures/vendor with that file"
    )
    # The vendored copy must be the bytes base.html's integrity pins, or the UI tests load a stale bundle.
    version = re.search(r"bootstrap@(\d+\.\d+\.\d+)/", url).group(1)
    fixture = ROOT / "tests" / "fixtures" / "vendor" / f"bootstrap-{version}.bundle.min.js"
    digest = "sha384-" + base64.b64encode(hashlib.sha384(fixture.read_bytes()).digest()).decode()
    assert digest in bundles[0][1]["integrity"].split(), (
        f"{fixture.relative_to(ROOT)} hashes to {digest}, not base.html's integrity; re-download it from {url}"
    )
