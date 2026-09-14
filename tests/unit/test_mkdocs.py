"""Tests for the MkDocs + GitHub Pages publishing setup (mkdocs.yml)."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_mkdocs_config():
    import io

    from mkdocs.utils import yaml as mkdocs_yaml

    return mkdocs_yaml.yaml_load(io.StringIO((REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")))


def _nav_files():
    """Yield every source filename referenced by the mkdocs nav, recursively."""
    config = _load_mkdocs_config()

    def walk(entries):
        for entry in entries:
            if isinstance(entry, dict):
                for key, value in entry.items():
                    if isinstance(value, str):
                        yield value
                    elif isinstance(value, list):
                        yield from walk(value)

    return list(walk(config["nav"]))


def test_mkdocs_nav_entries_point_to_existing_docs():
    """Every mkdocs nav entry maps to a markdown file that exists in docs/."""
    config = _load_mkdocs_config()
    docs_dir = REPO_ROOT / config["docs_dir"]

    files = _nav_files()
    assert files, "nav must reference at least one page"
    for filename in files:
        assert (docs_dir / filename).is_file(), f"nav entry missing file: {filename}"


def _build_site(tmp_path):
    """Build the docs site and return its root dir."""
    from click.testing import CliRunner
    from mkdocs.__main__ import build_command

    runner = CliRunner()
    site_dir = tmp_path / "site"
    result = runner.invoke(
        build_command,
        [
            "--strict",
            "--site-dir",
            str(site_dir),
            "--config-file",
            str(REPO_ROOT / "mkdocs.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
    return site_dir


def test_mkdocs_build_strict(tmp_path):
    """`mkdocs build --strict` completes with no warnings-as-errors.

    This is the same build the GitHub Pages workflow runs (gh-deploy --strict),
    so it catches broken relative links and missing nav files.
    """
    _build_site(tmp_path)


def test_mkdocs_missing_anchors_fail_strict(tmp_path):
    """Missing heading anchors are upgraded to warnings so `--strict` fails.

    MkDocs only emits INFO for undefinable anchor links by default, so a broken
    `[text](#bad-anchor)` link silently passes `mkdocs build --strict`. The
    `validation.links.anchors: warn` setting in mkdocs.yml escalates those to
    WARNING, which strict mode turns into a build failure.
    """
    config = _load_mkdocs_config()
    assert config["validation"]["links"]["anchors"] == "warn", (
        "validation.links.anchors must be 'warn' so broken anchor links fail strict builds"
    )


def test_mkdocs_internal_anchors_resolve(tmp_path):
    """Every same-page `#anchor` link in the markdown resolves to a real heading.

    MkDocs derives heading IDs (lowercase, `&` and punctuation stripped, spaces
    collapsed to single hyphens), so hand-written TOC links can drift from the
    generated IDs and silently render as dead links. Build the site once and
    check each link against the IDs actually emitted for its page.
    """
    import re

    config = _load_mkdocs_config()
    docs_dir = REPO_ROOT / config["docs_dir"]
    site_dir = _build_site(tmp_path)

    for src in sorted(docs_dir.rglob("*.md")):
        text = src.read_text(encoding="utf-8")
        links = re.findall(r"\[[^\]]*\]\(#([^()\s]+)\)", text)
        if not links:
            continue

        rel = src.relative_to(docs_dir)
        html = site_dir / rel.with_suffix("") / "index.html"
        assert html.is_file(), f"no built page for {rel}"
        ids = set(re.findall(r'id="([^"]+)"', html.read_text(encoding="utf-8")))

        for anchor in links:
            assert anchor in ids, f"{rel}: link '#{anchor}' has no matching heading id"


def test_mkdocs_mermaid_diagrams_render(tmp_path):
    """Mermaid blocks are emitted as `.mermaid` elements, not code fences.

    mkdocs.yml must register a `mermaid` custom SuperFences block; otherwise
    the `` ```mermaid `` fences in architecture.md/dbschema.md render as
    ordinary highlighted code blocks and the published pages show source
    instead of diagrams.
    """
    site_dir = _build_site(tmp_path)

    for page in ("architecture", "dbschema"):
        html = (site_dir / page / "index.html").read_text(encoding="utf-8")
        assert 'class="mermaid"' in html, f"{page}: expected a .mermaid element"
        assert 'language-mermaid' not in html, f"{page}: mermaid rendered as a code fence"
