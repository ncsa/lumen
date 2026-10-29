"""Every CDN <script>/<link> tag is pinned to an exact version and loaded with SRI.

A floating URL (``npm/marked/...``, ``chart.js@4``) lets an upstream release
change the UI without a code change, and a tag without ``integrity`` lets a
tampered CDN file run. This scans the app, theme and monitor templates offline;
it checks that the attributes are present and well formed, not that a hash is
correct (the browser enforces that, and the key-tables UI test does for the
Bootstrap bundle).
"""
import ast
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIRS = [
    ROOT / "lumen" / "templates",
    ROOT / "themes",
    ROOT / "scripts" / "monitor" / "monitor" / "templates",
]
CDN_HOSTS = ("https://cdn.jsdelivr.net/", "https://cdnjs.cloudflare.com/")
# jsDelivr: npm/<pkg>@1.2.3/...; cdnjs: ajax/libs/<lib>/1.2.3/...
EXACT_VERSION = re.compile(r"[@/]\d+\.\d+\.\d+/")
# sha384 is 48 bytes, so exactly 64 base64 characters and no padding.
SRI_SHA384 = re.compile(r"sha384-[A-Za-z0-9+/]{64}")
REGENERATE = "curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A"


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


def _cdn_tags():
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.rglob("*.html")):
            parser = _CdnTags()
            parser.feed(path.read_text())
            for lineno, url, attrs in parser.tags:
                yield f"{path.relative_to(ROOT)}:{lineno}", url, attrs


def test_cdn_tags_are_found():
    assert any(True for _ in _cdn_tags()), "no CDN tags found; did the template paths move?"


def test_cdn_tags_are_pinned_and_hashed():
    problems = []
    for where, url, attrs in _cdn_tags():
        if not EXACT_VERSION.search(url):
            problems.append(f"{where}: {url} is not pinned to an exact x.y.z version")
        if not SRI_SHA384.fullmatch(attrs.get("integrity") or ""):
            problems.append(f'{where}: {url} lacks a well-formed integrity="sha384-..."')
        if attrs.get("crossorigin") != "anonymous":
            problems.append(f'{where}: {url} lacks crossorigin="anonymous"')
    assert not problems, (
        "CDN tags must pin an exact version and carry SRI. Generate a hash with\n"
        f"  {REGENERATE}\n"
        "and cross-check it against a second source (the jsDelivr data API or the npm tarball).\n"
        + "\n".join(problems)
    )


def test_key_tables_bootstrap_url_matches_base_html():
    source = (ROOT / "tests" / "ui" / "test_key_tables.py").read_text()
    url = next(
        node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "BOOTSTRAP_JS_URL" for t in node.targets)
    )
    parser = _CdnTags()
    parser.feed((ROOT / "lumen" / "templates" / "base.html").read_text())
    base_urls = [u for _, u, _ in parser.tags if "bootstrap.bundle" in u]
    assert base_urls == [url], (
        f"BOOTSTRAP_JS_URL in tests/ui/test_key_tables.py ({url}) must match the bundle "
        f"base.html loads ({base_urls}); also replace tests/fixtures/vendor with that file"
    )
