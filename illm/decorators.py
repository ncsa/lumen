from functools import wraps
from flask import session, redirect, url_for, jsonify


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("entity_id"):
            return redirect(url_for("auth.landing"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("entity_id"):
            return redirect(url_for("auth.landing"))
        if not session.get("is_admin"):
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated
