import logging
import os
import sys

import yaml
from flask import Flask, session
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

    # Override Flask config from yaml app/oauth2 sections
    app_cfg = yaml_data.get("app", {})
    if "secret_key" in app_cfg:
        app.config["SECRET_KEY"] = app_cfg["secret_key"]
    if "database_url" in app_cfg:
        db_url = app_cfg["database_url"].replace("postgres://", "postgresql://", 1)
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    if "debug" in app_cfg:
        app.config["DEBUG"] = app_cfg["debug"]
    app.config["APP_NAME"] = app_cfg.get("name", "Lumen")
    app.config["APP_TAGLINE"] = app_cfg.get("tagline", "")

    oauth2_cfg = yaml_data.get("oauth2", {})
    for key in ("client_id", "client_secret", "server_metadata_url", "redirect_uri", "scopes"):
        if key in oauth2_cfg:
            app.config[f"OAUTH2_{key.upper()}"] = oauth2_cfg[key]
    if "params" in oauth2_cfg:
        app.config["OAUTH2_PARAMS"] = oauth2_cfg.get("params") or {}

    chat_cfg = yaml_data.get("chat", {})
    app.config["CHAT_CONVERSATION_REMOVE_MODE"] = chat_cfg.get("remove", "hide")

    logs_cfg = app_cfg.get("logs", {})
    if not logs_cfg.get("access", True):
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Initialize extensions
    from .extensions import db, migrate, oauth
    db.init_app(app)
    migrate.init_app(app, db)
    oauth.init_app(app)

    oauth.register(
        name="provider",
        client_id=app.config["OAUTH2_CLIENT_ID"],
        client_secret=app.config["OAUTH2_CLIENT_SECRET"],
        server_metadata_url=app.config["OAUTH2_SERVER_METADATA_URL"],
        client_kwargs={"scope": app.config["OAUTH2_SCOPES"]},
    )

    # Import all models so Flask-Migrate can detect them
    from . import models  # noqa: F401

    # Register blueprints
    from illm.blueprints.auth.routes import auth_bp
    from illm.blueprints.chat.routes import chat_bp
    from illm.blueprints.models_page.routes import models_page_bp
    from illm.blueprints.services.routes import services_bp
    from illm.blueprints.usage.routes import usage_bp
    from illm.blueprints.api.routes import api_bp
    from illm.blueprints.admin.routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(models_page_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(usage_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)

    # Register CLI commands
    from illm.commands import init_db_cmd
    app.cli.add_command(init_db_cmd)

    # Context processor: inject app_name and nav_services into all templates
    @app.context_processor
    def inject_nav():
        result = {"app_name": app.config["APP_NAME"], "app_tagline": app.config["APP_TAGLINE"]}
        if not session.get("entity_id"):
            result["nav_services"] = []
            return result
        from illm.models.entity_manager import EntityManager
        from illm.models.entity import Entity

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
    from illm.commands import sync_models_from_yaml, sync_groups_from_yaml
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

    # Start background health checker
    from illm.services.health import start_health_checker
    start_health_checker(app)

    # Start background token refiller
    from illm.services.token_refill import start_token_refiller
    start_token_refiller(app)

    # Start config file watcher
    from illm.services.config_watcher import start_config_watcher
    start_config_watcher(app, config_yaml_path)

    return app
