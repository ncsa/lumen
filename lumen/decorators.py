from functools import wraps
from http import HTTPStatus

from flask import session, redirect, url_for, jsonify, current_app

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


def is_admin(entity):
    yaml_data = current_app.config.get("YAML_DATA", {})
    return entity.email in yaml_data.get("admins", [])


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
            return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN
        return f(*args, **kwargs)
    return decorated
