"""The dashboard: a Flask blueprint mounted at `base_path`, no login.

Every route, the static files included, sits under the blueprint, so the whole
dashboard moves with `base_path` behind a reverse proxy. Templates build links
with `url_for`, and the page hands the API URLs to its JS. `base_path` is read
when the app is created; changing it needs a restart.
"""

from flask import Blueprint, Flask, current_app, jsonify, render_template, request

from monitor import overview
from monitor.buckets import RANGES, get_range
from monitor.config import ConfigStore
from monitor.db import utcnow
from monitor.probe import LiveSnapshot

bp = Blueprint("monitor", __name__, template_folder="templates", static_folder="static")


def _deps():
    return current_app.extensions["monitor"]


@bp.route("/")
def index():
    store, _, _ = _deps()
    rng = get_range(request.args.get("range"))
    return render_template(
        "overview.html",
        ranges=RANGES.values(),
        current_range=rng.key,
        refresh_page_ms=int(store.current.refresh.page.total_seconds() * 1000),
    )


@bp.route("/api/overview")
def api_overview():
    store, session_factory, snapshot = _deps()
    rng = get_range(request.args.get("range"))
    with session_factory() as session:
        data = overview.overview(session, store.current, snapshot, rng, utcnow(), request.args.get("sort"), request.args.get("dir"))
    return jsonify(data)


@bp.route("/api/lumen")
def api_lumen():
    store, session_factory, _ = _deps()
    rng = get_range(request.args.get("range"))
    with session_factory() as session:
        data = overview.lumen_card(session, store.current, rng, utcnow())
    return jsonify({"range": rng.key, **data})


def create_app(store: ConfigStore, session_factory, snapshot: LiveSnapshot | None = None) -> Flask:
    """The dashboard app, sharing the scheduler's config store, sessions and live snapshot."""
    # No app-level static route: the blueprint serves /static under base_path.
    app = Flask(__name__, static_folder=None)
    app.extensions["monitor"] = (store, session_factory, snapshot)
    app.register_blueprint(bp, url_prefix=store.current.base_path or None)
    return app
