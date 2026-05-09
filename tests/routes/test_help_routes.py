"""Tests for the help blueprint (/help/*)."""


def test_help_index_loads(client):
    resp = client.get("/help/")
    assert resp.status_code == 200
    assert b"html" in resp.data.lower()


def test_help_index_no_trailing_slash(client):
    resp = client.get("/help/", follow_redirects=True)
    assert resp.status_code == 200


def test_help_valid_slug(client):
    resp = client.get("/help/chat")
    assert resp.status_code == 200


def test_help_unknown_slug_returns_404(client):
    resp = client.get("/help/does-not-exist-ever")
    assert resp.status_code == 404


def test_help_img_serves_file(client):
    resp = client.get("/help/img/chat.png")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/")



def test_help_all_nav_slugs(client):
    """Every slug in nav.json should render without error."""
    from lumen.blueprints.help.routes import _SLUG_MAP
    for slug in _SLUG_MAP:
        url = "/help/" if slug == "" else f"/help/{slug}"
        resp = client.get(url)
        assert resp.status_code == 200, f"slug {slug!r} returned {resp.status_code}"


def test_rewrite_md_links_image():
    from pathlib import Path
    from lumen.blueprints.help.routes import _rewrite_md_links, DOCS_DIR

    intro = DOCS_DIR / "introduction.md"
    # Inline an img reference that points into docs/img/
    content = "![alt](img/chat.png)"
    result = _rewrite_md_links(content, intro)
    assert "/help/img/chat.png" in result


def test_rewrite_md_links_md_to_slug():
    from lumen.blueprints.help.routes import _rewrite_md_links, DOCS_DIR

    intro = DOCS_DIR / "introduction.md"
    content = "[Chat](guides/chat.md)"
    result = _rewrite_md_links(content, intro)
    assert "/help/chat" in result


def test_read_markdown_no_frontmatter():
    from lumen.blueprints.help.routes import _read_markdown, DOCS_DIR

    intro = DOCS_DIR / "introduction.md"
    title, content = _read_markdown(intro)
    assert isinstance(title, str)
    assert isinstance(content, str)


def test_read_markdown_with_frontmatter(tmp_path):
    """_read_markdown correctly parses YAML frontmatter and extracts title."""
    from lumen.blueprints.help.routes import _read_markdown

    md = tmp_path / "test.md"
    md.write_text('---\ntitle: "My Page"\n---\n\nBody content here.\n', encoding="utf-8")
    title, content = _read_markdown(md)
    assert title == "My Page"
    assert "Body content here" in content


def test_read_markdown_malformed_frontmatter(tmp_path):
    """A file starting with --- but missing closing --- falls back to raw content."""
    from lumen.blueprints.help.routes import _read_markdown

    md = tmp_path / "test.md"
    md.write_text("---\ntitle: broken\n", encoding="utf-8")
    title, content = _read_markdown(md)
    assert title == ""
    assert "broken" in content


def test_rewrite_md_links_unknown_image(tmp_path):
    """An image link not under _IMG_DIR is left unchanged."""
    from lumen.blueprints.help.routes import _rewrite_md_links

    fake_file = tmp_path / "page.md"
    fake_file.write_text("", encoding="utf-8")
    content = "![alt](../somewhere/image.png)"
    result = _rewrite_md_links(content, fake_file)
    assert result == content
