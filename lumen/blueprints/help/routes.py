import json
import re
from http import HTTPStatus
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, send_file, url_for

help_bp = Blueprint("help_bp", __name__, url_prefix="/help")

DOCS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "docs"

DOC_NAV = json.loads((DOCS_DIR / "nav.json").read_text(encoding="utf-8"))


def _nav_entries():
    """Yield (slug, section, filename) for every doc page."""
    for entry in DOC_NAV:
        section = entry.get("section", "")
        if entry.get("items"):
            for item in entry["items"]:
                yield item["slug"], section, item["filename"]
        else:
            yield entry["slug"], section, entry.get("filename", "")


def _resolve_file(section, filename):
    """Return the filesystem path for a section/filename pair, or None."""
    for candidate in [DOCS_DIR / section / filename, DOCS_DIR / filename]:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _build_maps():
    slug_to_file = {}
    file_to_slug = {}
    for slug, section, filename in _nav_entries():
        if not filename:
            continue
        path = _resolve_file(section, filename)
        if path:
            slug_to_file[slug] = path
            file_to_slug[path] = slug
    return slug_to_file, file_to_slug


_SLUG_MAP, _FILE_SLUG_MAP = _build_maps()

_LINK_RE = re.compile(r'(!?)\[([^\]]*)\]\((?!https?://|/)([^)]+)\)')
_IMG_DIR = DOCS_DIR / "img"


def _slug_url(slug):
    return "/help/" if slug == "" else f"/help/{slug}"


def _rewrite_md_links(content, current_file):
    """Rewrite relative links to absolute URLs: .md → /help/<slug>, images → /help/img/<name>."""
    base_dir = current_file.parent

    def replace(m):
        bang, text, href = m.group(1), m.group(2), m.group(3)
        href_path, _, fragment = href.partition("#")
        abs_target = (base_dir / href_path).resolve()
        if bang:
            if abs_target.parent == _IMG_DIR:
                return f"![{text}](/help/img/{abs_target.name})"
            return m.group(0)
        slug = _FILE_SLUG_MAP.get(abs_target)
        if slug is None:
            return m.group(0)
        suffix = f"#{fragment}" if fragment else ""
        return f"[{text}]({_slug_url(slug)}{suffix})"

    return _LINK_RE.sub(replace, content)


def _read_markdown(path):
    """Read the file and parse frontmatter if present."""
    raw = path.read_text(encoding="utf-8")
    title = ""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].split("\n"):
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"').strip("'")
            content = parts[2]
        else:
            content = raw
    else:
        content = raw
    return title, _rewrite_md_links(content, path)


@help_bp.route("/img/<path:filename>")
def img(filename):
    return send_file(DOCS_DIR / "img" / filename)


@help_bp.route("/")
def index_page():
    path = _SLUG_MAP.get("")
    if path is None:
        abort(HTTPStatus.NOT_FOUND)
    title, content = _read_markdown(path)
    return render_template("help.html", title=title, page_content=content,
                           sections=DOC_NAV, current_slug="")


@help_bp.route("/<path:slug>")
def page(slug):
    path = _SLUG_MAP.get(slug)
    if path is None:
        abort(HTTPStatus.NOT_FOUND)
    title, content = _read_markdown(path)
    return render_template("help.html", title=title, page_content=content,
                           sections=DOC_NAV, current_slug=slug)
