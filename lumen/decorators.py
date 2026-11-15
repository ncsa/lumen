from functools import wraps
from http import HTTPStatus
from uuid import UUID

from flask import current_app, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.entity import Entity


def load_logged_in_entity():
    """before_request hook: resolve the session's ``entity_uuid`` once per request.

    Sets ``g.entity`` (the user row, or None) and ``g.entity_id`` (its integer
    id, server-side only, or None). The cookie carries only the opaque UUID; a
    cookie without ``entity_uuid`` (e.g. one written before the switch, holding
    the old integer ``entity_id``) counts as logged out. Active status is checked
    by the decorators, not here.
    """
    g.entity = None
    g.entity_id = None
    raw = session.get("entity_uuid")
    if not raw:
        return
    try:
        u = UUID(str(raw))
    except ValueError:
        return
    entity = db.session.execute(
        select(Entity).where(Entity.uuid == u, Entity.entity_type == "user")
    ).scalar_one_or_none()
    if entity is not None:
        g.entity = entity
        g.entity_id = entity.id


def login_session(entity):
    """Write the session for a user who just signed in; the cookie holds only the UUID.

    Drops the old integer ``entity_id`` and any OAuth consent binding left in the
    cookie: a consent page rendered before this login belonged to an earlier
    session (and, from before the UUID switch, holds integer request ids). The
    consent page is rendered again after login, which binds it afresh;
    ``oauth_return_to`` stays so the login round trip still lands there.
    """
    for key in ("entity_id", "oauth_consent_request_id", "oauth_consent_state"):
        session.pop(key, None)
    session["entity_uuid"] = str(entity.uuid)
    session["entity_name"] = entity.name
    session["initials"] = entity.initials
    session["gravatar_hash"] = entity.gravatar_hash or ""
    session["entity_email"] = entity.email


def _clear_stale_login():
    """Drop a cookie that names a user who is gone, inactive, or in the old integer format."""
    if session.get("entity_uuid") or session.get("entity_id"):
        session.clear()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        entity = g.get("entity")
        if not entity or not entity.active:
            _clear_stale_login()
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
        entity = g.get("entity")
        if not entity or not entity.active:
            _clear_stale_login()
            return redirect(url_for("auth.landing"))
        if not is_admin(entity):
            if request.accept_mimetypes.best_match(["application/json", "text/html"]) == "application/json":
                return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN
            return render_template("errors/403.html"), HTTPStatus.FORBIDDEN
        return f(*args, **kwargs)
    return decorated
