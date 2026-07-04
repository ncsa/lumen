import hashlib
import logging
import os
import sys
from http import HTTPStatus

import yaml
from flask import Flask, g, jsonify, render_template, request, session
from sqlalchemy import text
from jinja2 import BaseLoader, ChoiceLoader, TemplateNotFound
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix


class _ThemeLoader(BaseLoader):
    """Jinja2 loader that resolves templates from the active theme directory at render time."""

    def __init__(self, themes_root, app):
        self._themes_root = themes_root
        self._app = app

    def get_source(self, environment, template):
        try:
            theme_name = g.theme_name
        except RuntimeError:
            theme_name = self._app.config.get("THEME_NAME", "default")
        path = os.path.join(self._themes_root, theme_name, "templates", template)
        if not os.path.isfile(path):
            raise TemplateNotFound(template)
        mtime = os.path.getmtime(path)
        with open(path) as f:
            source = f.read()
        _app = self._app
        _themes_root = self._themes_root

        def uptodate():
            try:
                current = g.theme_name
            except RuntimeError:
                current = _app.config.get("THEME_NAME", "default")
            expected = os.path.join(_themes_root, current, "templates", template)
            return expected == path and mtime == os.path.getmtime(path)

        return source, path, uptodate


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    from config import Config
    app.config.from_object(Config)

    # Validate config.yaml at startup
    config_yaml_path = app.config["CONFIG_YAML"]
    if not os.path.exists(config_yaml_path):
        print(
            f"ERROR: config.yaml not found at '{config_yaml_path}'. App cannot start.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(config_yaml_path) as f:
        yaml_data = yaml.safe_load(f)

    # A model is active unless explicitly disabled (or the legacy active: false).
    active_models = [
        m for m in yaml_data.get("models", [])
        if not m.get("disabled", False) and m.get("active", True)
    ]
    if not active_models:
        print(
            "ERROR: config.yaml contains no active models. App cannot start.",
            file=sys.stderr,
        )
        sys.exit(1)

    app.config["YAML_DATA"] = yaml_data

    # Initialize Prometheus registry with DB collector
    from prometheus_client import CollectorRegistry
    from lumen.blueprints.metrics.routes import LumenDBCollector
    prom_registry = CollectorRegistry()
    prom_registry.register(LumenDBCollector())
    app.config["PROMETHEUS_REGISTRY"] = prom_registry

    # Wrap wsgi_app with HTTP metrics middleware if prometheus is enabled.
    # PROMETHEUS_MULTIPROC_DIR must be set before prometheus_client metric objects
    # are created (imported), so we set it here before importing the middleware.
    prom_cfg = yaml_data.get("api", {}).get("prometheus", {})
    if prom_cfg.get("enabled", False) and not prom_cfg.get("token", ""):
        app.logger.error(
            "prometheus is enabled but api.prometheus.token is not set; disabling Prometheus."
        )
        prom_cfg = {**prom_cfg, "enabled": False}
        api_cfg_updated = {**yaml_data.get("api", {}), "prometheus": prom_cfg}
        yaml_data = {**yaml_data, "api": api_cfg_updated}
        app.config["YAML_DATA"] = yaml_data
    if prom_cfg.get("enabled", False):
        multiproc_dir = prom_cfg.get("multiproc_dir", "")
        if multiproc_dir:
            os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", multiproc_dir)
        from lumen.blueprints.metrics.middleware import make_metrics_middleware
        app.wsgi_app = make_metrics_middleware(app.wsgi_app)

    # Configure rate limiting
    rl_cfg = yaml_data.get("rate_limiting", {})
    if rl_url := rl_cfg.get("storage_url"):
        app.config["RATELIMIT_STORAGE_URI"] = rl_url
    else:
        app.logger.warning(
            "rate_limiting.storage_url is not set; rate limits are per-worker and ineffective "
            "in multi-worker deployments. Set to a Redis URL for shared rate limiting."
        )

    # Override Flask config from yaml app/oauth2 sections
    app_cfg = yaml_data.get("app", {})
    secret_key = os.environ.get("LUMEN_SECRET_KEY") or app_cfg.get("secret_key", "")
    if not secret_key:
        app.logger.error("app.secret_key is not set in config.yaml (or LUMEN_SECRET_KEY env var). App cannot start.")
        sys.exit(1)
    app.config["SECRET_KEY"] = secret_key
    encryption_key = os.environ.get("LUMEN_ENCRYPTION_KEY") or app_cfg.get("encryption_key", "")
    if not encryption_key:
        app.logger.error("app.encryption_key is not set in config.yaml (or LUMEN_ENCRYPTION_KEY env var). App cannot start.")
        sys.exit(1)
    app.config["ENCRYPTION_KEY"] = encryption_key
    db_cfg = app_cfg.get("database", {})
    if db_cfg.get("url") and not os.environ.get("DATABASE_URL"):
        db_url = db_cfg["url"].replace("postgres://", "postgresql://", 1)
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    from lumen.services.db_pool import build_engine_options, detect_replicas, detect_workers
    engine_options = build_engine_options(
        app.config["SQLALCHEMY_DATABASE_URI"],
        db_cfg,
        workers=detect_workers(),
        replicas=detect_replicas(),
    )
    if engine_options:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
    if "debug" in app_cfg:
        app.config["DEBUG"] = app_cfg["debug"]
    app.config["SESSION_COOKIE_SECURE"] = not app.debug
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = 86400
    from lumen.services.config_watcher import apply_hot_config, _apply_theme
    apply_hot_config(app, yaml_data)
    app.config["APP_VERSION"] = os.environ.get("APP_VERSION", "develop")
    app.config["GIT_COMMIT"] = os.environ.get("GIT_COMMIT", "N/A")

    logs_cfg = app_cfg.get("logs", {})
    log_level = getattr(logging, logs_cfg.get("level", "INFO").upper(), logging.INFO)
    app.logger.setLevel(log_level)

    # Always check template freshness so per-request theme switching reloads theme templates
    # when the active theme changes between requests.
    app.jinja_env.auto_reload = True

    # Load theme — dynamic loader so hot reload works when app.theme changes in config.yaml
    themes_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "themes")
    app.config["THEMES_ROOT"] = themes_root
    # Pre-load all valid theme configs for fast per-request skin override lookups
    _theme_cache: dict = {}
    for _entry in os.scandir(themes_root):
        if _entry.is_dir():
            _theme_yaml = os.path.join(_entry.path, "theme.yaml")
            if os.path.isfile(_theme_yaml):
                with open(_theme_yaml) as _f:
                    _theme_cache[_entry.name] = yaml.safe_load(_f)
    app.config["THEME_CACHE"] = _theme_cache
    app.jinja_loader = ChoiceLoader([_ThemeLoader(themes_root, app), app.jinja_loader])
    _apply_theme(app, yaml_data)
    if not app.config.get("THEME_NAME"):
        # Requested theme not found; fall back to default
        _apply_theme(app, {"app": {"theme": "default"}})

    from flask import send_from_directory as _send_from_directory

    @app.before_request
    def apply_theme_for_request():
        cache = app.config.get("THEME_CACHE", {})
        email = session.get("entity_email", "")
        if email:
            email_themes = app.config.get("EMAIL_THEMES", {})
            for pattern, theme_name in email_themes.items():
                if theme_name in cache:
                    if pattern.startswith("@") and email.lower().endswith(pattern.lower()):
                        g.theme = cache[theme_name]
                        g.theme_name = theme_name
                        return
                    elif email.lower() == pattern.lower():
                        g.theme = cache[theme_name]
                        g.theme_name = theme_name
                        return
        g.theme = app.config["THEME"]
        g.theme_name = app.config["THEME_NAME"]

    @app.route("/theme-static/<path:filename>")
    def theme_static(filename):
        theme_dir = os.path.join(app.config["THEMES_ROOT"], g.theme_name, "static")
        return _send_from_directory(theme_dir, filename)

    oauth2_cfg = yaml_data.get("oauth2", {})
    for key in ("client_id", "client_secret", "server_metadata_url", "redirect_uri", "scopes"):
        env_val = os.environ.get(f"OAUTH2_{key.upper()}")
        if env_val:
            app.config[f"OAUTH2_{key.upper()}"] = env_val
        elif key in oauth2_cfg:
            app.config[f"OAUTH2_{key.upper()}"] = oauth2_cfg[key]

    # Initialize extensions
    from .extensions import db, migrate, oauth, limiter
    db.init_app(app)
    migrate.init_app(app, db)
    oauth.init_app(app)
    limiter.init_app(app)

    from flask_wtf.csrf import CSRFProtect
    csrf = CSRFProtect(app)
    if rl_cfg.get("storage_url"):
        app.logger.warning(
            "rate_limiting.storage_url requires a restart to take effect; it is not hot-reloaded."
        )

    if app.config["DEV_USER"]:
        if app.config["SESSION_COOKIE_SECURE"]:
            app.logger.error(
                "DEV_USER is set (%s) but the app is running in production mode. "
                "Refusing to start — DEV_USER must not be used in production.",
                app.config["DEV_USER"],
            )
            sys.exit(1)
        app.logger.warning("DEV_USER is set (%s). OAuth is bypassed. DO NOT use in production.",
                           app.config["DEV_USER"])

    if app.config.get("OAUTH2_CLIENT_ID"):
        oauth.register(
            name="provider",
            client_id=app.config["OAUTH2_CLIENT_ID"],
            client_secret=app.config["OAUTH2_CLIENT_SECRET"],
            server_metadata_url=app.config["OAUTH2_SERVER_METADATA_URL"],
            client_kwargs={"scope": app.config["OAUTH2_SCOPES"]},
        )
    elif not app.config["DEV_USER"]:
        app.logger.error("Neither oauth2.client_id nor app.dev_user is configured. App cannot start.")
        sys.exit(1)

    # Import all models so Flask-Migrate can detect them
    from . import models  # noqa: F401

    # Register blueprints
    from lumen.blueprints.auth.routes import auth_bp
    from lumen.blueprints.chat.routes import chat_bp
    from lumen.blueprints.models_page.routes import models_page_bp
    from lumen.blueprints.clients.routes import clients_bp
    from lumen.blueprints.profile.routes import profile_bp
    from lumen.blueprints.api.routes import api_bp
    from lumen.blueprints.admin.routes import admin_bp
    from lumen.blueprints.metrics.routes import metrics_bp
    from lumen.blueprints.help.routes import help_bp
    from lumen.blueprints.connect.routes import connect_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(models_page_bp)
    app.register_blueprint(clients_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(api_bp)
    csrf.exempt(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(help_bp)
    app.register_blueprint(connect_bp)

    @app.route("/healthz")
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:
            return "", HTTPStatus.SERVICE_UNAVAILABLE
        return "", HTTPStatus.OK

    @app.after_request
    def set_security_headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not app.debug:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    # Rate limit error handler
    @app.errorhandler(HTTPStatus.TOO_MANY_REQUESTS)
    def ratelimit_handler(e):
        if request.path.startswith("/v1/"):
            return jsonify({"error": {"message": "Rate limit exceeded. Please slow down.",
                                       "type": "rate_limit_error", "code": "rate_limit_exceeded"}}), HTTPStatus.TOO_MANY_REQUESTS
        return jsonify({"error": "Rate limit exceeded. Please slow down."}), HTTPStatus.TOO_MANY_REQUESTS

    # Friendly themed pages for not-found and server errors (JSON for API clients).
    @app.errorhandler(HTTPStatus.NOT_FOUND)
    def not_found_handler(e):
        if request.path.startswith("/v1/"):
            return jsonify({"error": {"message": "Not found", "type": "invalid_request_error",
                                       "code": "not_found"}}), HTTPStatus.NOT_FOUND
        return render_template("errors/404.html"), HTTPStatus.NOT_FOUND

    @app.errorhandler(HTTPStatus.INTERNAL_SERVER_ERROR)
    def server_error_handler(e):
        if request.path.startswith("/v1/"):
            return jsonify({"error": {"message": "Internal server error", "type": "api_error",
                                       "code": "internal_error"}}), HTTPStatus.INTERNAL_SERVER_ERROR
        return render_template("errors/500.html"), HTTPStatus.INTERNAL_SERVER_ERROR

    # Register CLI commands
    from lumen.commands import init_db_cmd, reassign_model_cmd
    app.cli.add_command(init_db_cmd)
    app.cli.add_command(reassign_model_cmd)

    # Context processor: inject app_name and nav_clients into all templates
    @app.context_processor
    def inject_nav():
        result = {
            "app_name": app.config["APP_NAME"],
            "app_tagline": app.config["APP_TAGLINE"],
            "app_announcement": app.config.get("APP_ANNOUNCEMENT", Markup("")),
            "app_announcement_key": hashlib.md5(str(app.config.get("APP_ANNOUNCEMENT", "")).encode(), usedforsecurity=False).hexdigest()[:16],
            "is_admin": False,
            "github_url": app.config.get("GITHUB_URL", ""),
            "app_version": app.config.get("APP_VERSION", "develop"),
            "git_commit": app.config.get("GIT_COMMIT", "N/A"),
            "is_logged_in": bool(session.get("entity_id")),
            "theme": g.theme,
        }
        if not session.get("entity_id"):
            result["nav_clients"] = []
            return result

        # Cache is_admin and client membership in the session to avoid 3 DB queries per request.
        # Cache is populated on first request after login and cleared on logout.
        nav_cache = session.get("_nav")
        if nav_cache is not None:
            result["is_admin"] = nav_cache["is_admin"]
            result["nav_clients"] = nav_cache["client_ids"]
            return result

        from sqlalchemy import select
        from lumen.models.entity_manager import EntityManager
        from lumen.models.entity import Entity
        from lumen.extensions import db
        from lumen.decorators import is_admin as _is_admin
        entity = db.session.get(Entity, session["entity_id"])
        is_admin_val = _is_admin(entity) if entity else False
        result["is_admin"] = is_admin_val

        assocs = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=session["entity_id"])
        ).scalars().all()
        client_ids = [a.client_entity_id for a in assocs]
        if client_ids:
            clients = db.session.execute(
                select(Entity)
                .where(
                    Entity.id.in_(client_ids),
                    Entity.entity_type == "client",
                    Entity.active == True,
                )
                .order_by(Entity.name)
            ).scalars().all()
            active_client_ids = [c.id for c in clients]
        else:
            active_client_ids = []

        session["_nav"] = {"is_admin": is_admin_val, "client_ids": active_client_ids}
        result["nav_clients"] = active_client_ids
        return result

    # Register markdown Jinja2 filter
    import markdown as _markdown
    _md = _markdown.Markdown(extensions=["tables", "fenced_code", "toc", "codehilite"])

    def _md_filter(text):
        # WARNING: output is marked safe — never apply to user-supplied content.
        # Only use on operator-controlled fields (e.g. mc.notice from config.yaml).
        _md.reset()
        return Markup(_md.convert(text or ""))

    app.jinja_env.filters["markdown"] = _md_filter

    # Sync models, groups, and clients from yaml into DB on every startup
    from lumen.commands import backfill_clients_to_config, sync_clients_from_yaml, sync_groups_from_yaml, sync_models_from_yaml, sync_user_groups_from_yaml, sync_user_limits_from_yaml
    with app.app_context():
        try:
            sync_models_from_yaml(yaml_data)
        except Exception as e:
            print(f"WARNING: Could not sync models from yaml (run 'flask db upgrade' first): {e}",
                  file=sys.stderr)
        try:
            sync_groups_from_yaml(yaml_data)
        except Exception as e:
            print(f"WARNING: Could not sync groups from yaml (run 'flask db upgrade' first): {e}",
                  file=sys.stderr)
        try:
            sync_user_groups_from_yaml(yaml_data)
        except Exception as e:
            print(f"WARNING: Could not sync user groups from yaml (run 'flask db upgrade' first): {e}",
                  file=sys.stderr)
        try:
            sync_user_limits_from_yaml(yaml_data)
        except Exception as e:
            print(f"WARNING: Could not sync user limits from yaml (run 'flask db upgrade' first): {e}",
                  file=sys.stderr)
        try:
            # Self-heal config.yaml for installs whose clients pre-date write-on-create.
            if app.config.get("CONFIG_EDITOR", True) and os.access(config_yaml_path, os.W_OK):
                backfill_clients_to_config(yaml_data, config_yaml_path)
        except Exception as e:
            print(f"WARNING: Could not backfill clients to config.yaml: {e}", file=sys.stderr)
        try:
            sync_clients_from_yaml(yaml_data)
        except Exception as e:
            print(f"WARNING: Could not sync clients from yaml (run 'flask db upgrade' first): {e}",
                  file=sys.stderr)

    # Start background threads only in the main worker process.
    # - Werkzeug dev server: double-imports the app; only run in the child (WERKZEUG_RUN_MAIN=true).
    # - Uvicorn: create_app() is only called in worker processes, always run unless
    #   BACKGROUND_WORKER=false is set (use this to disable on extra workers).
    if os.environ.get("WERKZEUG_RUN_MAIN") is not None:
        _run_background = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    else:
        _run_background = os.environ.get("BACKGROUND_WORKER", "true") != "false"

    if _run_background:
        from lumen.services.health import start_health_checker
        start_health_checker(app)

        from lumen.services.token_refill import start_coin_refiller
        start_coin_refiller(app)

        from lumen.services.config_watcher import start_config_watcher
        start_config_watcher(app, config_yaml_path)

    return app
