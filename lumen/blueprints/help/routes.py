import os
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, send_file, url_for

help_bp = Blueprint("help_bp", __name__, url_prefix="/help")

DOCS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "docs"


def _resolve_path(section, filename):
    """Resolve a docs path like 'guides/chat' or 'models/model-detail'."""
    if not section:
        return DOCS_DIR / "index.md"
    path = DOCS_DIR / section / filename
    if not path.is_file():
        path = DOCS_DIR / section.lower() / filename
    if not path.is_file():
        fallback = DOCS_DIR / filename
        if fallback.is_file():
            return fallback
        abort(404)
    return path


def _read_markdown(path):
    """Read the file and parse frontmatter if present."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n", 1)
    title = ""
    if raw.startswith("---") and len(lines) > 1:
        rest = lines[1]
        parts = rest.split("---", 1)
        if len(parts) == 2:
            for tl in parts[0].split("\n"):
                if tl.startswith("title:"):
                    title = tl.split(":", 1)[1].strip().strip('"').strip("'")
            content = parts[1]
        else:
            content = raw
    else:
        content = raw
    return title, content


# Map URL paths to (filename, title)
DOC_NAV = [
    {
        "section": "introduction",
        "title": "Introduction",
        "filename": "introduction.md",
        "icon": "info-circle",
    },
    {
        "section": "guides",
        "title": "Guides",
        "items": [
            {"title": "Chat", "filename": "chat.md", "icon": "chat"},
            {"title": "Usage & API Keys", "filename": "usage.md", "icon": "pie-chart"},
            {"title": "API Reference", "filename": "api-reference.md", "icon": "key"},
        ],
    },
    {
        "section": "models",
        "title": "Models",
        "items": [
            {"title": "Model Dashboard", "filename": "models.md", "icon": "grid"},
            {"title": "Model Detail", "filename": "model-detail.md", "icon": "card-list"},
        ],
    },
    {
        "section": "clients",
        "title": "Clients",
        "items": [
            {"title": "Clients Overview", "filename": "clients.md", "icon": "people"},
            {"title": "Client Management", "filename": "clients-detail.md", "icon": "sliders"},
        ],
    },
]


@help_bp.route("/img/<path:filename>")
def img(filename):
    img_dir = DOCS_DIR / "img"
    return send_file(img_dir / filename)


@help_bp.route("/")
def index_page():
    return redirect(url_for("help_bp.page", filename="introduction/introduction.md"))


@help_bp.route("/<path:filename>")
def page(filename):
    if "/" in filename:
        parts = filename.rsplit("/", 1)
        section, fname = parts[0], parts[1]
    else:
        section, fname = "", filename
    path = _resolve_path(section, fname)
    title, content = _read_markdown(path)
    return render_template("help.html", title=title, page_content=content,
                           sections=DOC_NAV, current_page=filename)
