from functools import wraps
from http import HTTPStatus

from flask import current_app, jsonify, redirect, render_template, request, session, url_for

from lumen.extensions import db
from lumen.models.entity import Entity


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("entity_id"):
            return redirect(url_for("auth.landing"))
        entity = db.session.get(Entity, session["entity_id"])
        if not entity or not entity.active:
            session.clear()
            return redirect(url_for("auth.landing"))
        return f(*args, **kwargs)
    return decorated


def is_admin_eligible(entity):
    """Config-level admin eligibility (the YAML admins list), independent of admin mode."""
    yaml_data = current_app.config.get("YAML_DATA", {})
    return entity is not None and entity.email in yaml_data.get("admins", [])


def is_admin(entity):
    """Effective admin: config-eligible AND admin mode enabled for this session.

    Requires a request context (reads the session). Use is_admin_eligible for
    the context-free config check.
    """
    return bool(session.get("admin_mode")) and is_admin_eligible(entity)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("entity_id"):
            return redirect(url_for("auth.landing"))
        entity = db.session.get(Entity, session["entity_id"])
        if not entity or not entity.active:
            session.clear()
            return redirect(url_for("auth.landing"))
        if not is_admin(entity):
            if request.accept_mimetypes.best_match(["application/json", "text/html"]) == "application/json":
                return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN
            return render_template("errors/403.html"), HTTPStatus.FORBIDDEN
        return f(*args, **kwargs)
    return decorated
