import logging
import os
import sys

import yaml
from flask import Flask, jsonify, request, session
from werkzeug.middleware.proxy_fix import ProxyFix


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

    active_models = [m for m in yaml_data.get("models", []) if m.get("active", True)]
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
    prom_cfg = yaml_data.get("prometheus", {})
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

    # Override Flask config from yaml app/oauth2 sections
    app_cfg = yaml_data.get("app", {})
    secret_key = app_cfg.get("secret_key", "")
    if not secret_key:
        app.logger.error("app.secret_key is not set in config.yaml. App cannot start.")
        sys.exit(1)
    app.config["SECRET_KEY"] = secret_key
    encryption_key = os.environ.get("LUMEN_ENCRYPTION_KEY") or app_cfg.get("encryption_key", "")
    if not encryption_key:
        app.logger.error("app.encryption_key is not set in config.yaml (or LUMEN_ENCRYPTION_KEY env var). App cannot start.")
        sys.exit(1)
    app.config["ENCRYPTION_KEY"] = encryption_key
    if "database_url" in app_cfg and not os.environ.get("DATABASE_URL"):
        db_url = app_cfg["database_url"].replace("postgres://", "postgresql://", 1)
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    db_pool = app_cfg.get("db_pool", {})
    if db_pool:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            k: db_pool[k]
            for k in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle", "pool_pre_ping")
            if k in db_pool
        }
    if "debug" in app_cfg:
        app.config["DEBUG"] = app_cfg["debug"]
    app.config["APP_NAME"] = app_cfg.get("name", "Lumen")
    app.config["APP_TAGLINE"] = app_cfg.get("tagline", "")
    app.config["DEV_USER"] = app_cfg.get("dev_user", "")
    app.config["GITHUB_URL"] = app_cfg.get("github_url", "https://github.com/ncsa/lumen")

    oauth2_cfg = yaml_data.get("oauth2", {})
    for key in ("client_id", "client_secret", "server_metadata_url", "redirect_uri", "scopes"):
        if key in oauth2_cfg:
            app.config[f"OAUTH2_{key.upper()}"] = oauth2_cfg[key]
    if "params" in oauth2_cfg:
        app.config["OAUTH2_PARAMS"] = oauth2_cfg.get("params") or {}

    chat_cfg = yaml_data.get("chat", {})
    app.config["CHAT_CONVERSATION_REMOVE_MODE"] = chat_cfg.get("remove", "hide")

    logs_cfg = app_cfg.get("logs", {})
    log_level = getattr(logging, logs_cfg.get("level", "INFO").upper(), logging.INFO)
    app.logger.setLevel(log_level)
    if not logs_cfg.get("access", True):
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    app.config["LOG_MODEL_HEALTH"] = logs_cfg.get("model", False)

    # Initialize extensions
    from .extensions import db, migrate, oauth, limiter
    db.init_app(app)
    migrate.init_app(app, db)
    oauth.init_app(app)
    limiter.init_app(app)
    if rl_cfg.get("storage_url"):
        app.logger.warning(
            "rate_limiting.storage_url requires a restart to take effect; it is not hot-reloaded."
        )

    if app.config["DEV_USER"]:
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
    from lumen.blueprints.services.routes import services_bp
    from lumen.blueprints.usage.routes import usage_bp
    from lumen.blueprints.api.routes import api_bp
    from lumen.blueprints.admin.routes import admin_bp
    from lumen.blueprints.metrics.routes import metrics_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(models_page_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(usage_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(metrics_bp)

    # Rate limit error handler
    @app.errorhandler(429)
    def ratelimit_handler(e):
        if request.path.startswith("/v1/"):
            return jsonify({"error": {"message": "Rate limit exceeded. Please slow down.",
                                       "type": "rate_limit_error", "code": "rate_limit_exceeded"}}), 429
        return jsonify({"error": "Rate limit exceeded. Please slow down."}), 429

    # Register CLI commands
    from lumen.commands import init_db_cmd, reassign_model_cmd
    app.cli.add_command(init_db_cmd)
    app.cli.add_command(reassign_model_cmd)

    # Context processor: inject app_name and nav_services into all templates
    @app.context_processor
    def inject_nav():
        result = {"app_name": app.config["APP_NAME"], "app_tagline": app.config["APP_TAGLINE"], "is_admin": False, "github_url": app.config.get("GITHUB_URL", "")}
        if not session.get("entity_id"):
            result["nav_services"] = []
            return result
        from lumen.models.entity_manager import EntityManager
        from lumen.models.entity import Entity

        from lumen.decorators import is_admin
        entity = Entity.query.get(session["entity_id"])
        if entity:
            result["is_admin"] = is_admin(entity)

        assocs = EntityManager.query.filter_by(user_entity_id=session["entity_id"]).all()
        service_ids = [a.service_entity_id for a in assocs]
        if not service_ids:
            result["nav_services"] = []
            return result
        services = (
            Entity.query.filter(
                Entity.id.in_(service_ids),
                Entity.entity_type == "service",
                Entity.active == True,
            )
            .order_by(Entity.name)
            .all()
        )
        result["nav_services"] = services
        return result

    # Sync models and groups from yaml into DB on every startup
    from lumen.commands import sync_models_from_yaml, sync_groups_from_yaml
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
