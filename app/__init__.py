import os
import sys

import yaml
from flask import Flask, session


def create_app():
    app = Flask(__name__)

    from config import Config
    app.config.from_object(Config)

    # Validate models.yaml at startup
    models_yaml_path = app.config["MODELS_YAML_PATH"]
    if not os.path.exists(models_yaml_path):
        print(
            f"ERROR: models.yaml not found at '{models_yaml_path}'. App cannot start.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(models_yaml_path) as f:
        yaml_data = yaml.safe_load(f)

    active_models = [m for m in yaml_data.get("models", []) if m.get("active", True)]
    if not active_models:
        print(
            "ERROR: models.yaml contains no active models. App cannot start.",
            file=sys.stderr,
        )
        sys.exit(1)

    app.config["YAML_DATA"] = yaml_data

    chat_cfg = yaml_data.get("chat", {})
    app.config["CHAT_CONVERSATION_REMOVE_MODE"] = chat_cfg.get("remove", "hide")

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
    from app.blueprints.auth.routes import auth_bp
    from app.blueprints.chat.routes import chat_bp
    from app.blueprints.models_page.routes import models_page_bp
    from app.blueprints.services.routes import services_bp
    from app.blueprints.usage.routes import usage_bp
    from app.blueprints.api.routes import api_bp
    from app.blueprints.admin.routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(models_page_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(usage_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)

    # Register CLI commands
    from app.commands import init_db_cmd
    app.cli.add_command(init_db_cmd)

    # Context processor: inject nav_services into all templates
    @app.context_processor
    def inject_nav():
        if not session.get("entity_id"):
            return {"nav_services": []}
        from app.models.entity_manager import EntityManager
        from app.models.entity import Entity

        assocs = EntityManager.query.filter_by(user_entity_id=session["entity_id"]).all()
        service_ids = [a.service_entity_id for a in assocs]
        if not service_ids:
            return {"nav_services": []}
        services = (
            Entity.query.filter(
                Entity.id.in_(service_ids),
                Entity.entity_type == "service",
                Entity.active == True,
            )
            .order_by(Entity.name)
            .all()
        )
        return {"nav_services": services}

    # Sync models from yaml into DB on every startup
    from app.commands import sync_models_from_yaml
    with app.app_context():
        try:
            sync_models_from_yaml(yaml_data)
        except Exception as e:
            print(f"WARNING: Could not sync models from yaml (run 'flask db upgrade' first): {e}",
                  file=sys.stderr)

    # Start background health checker
    from app.services.health import start_health_checker
    start_health_checker(app)

    # Start background token refiller
    from app.services.token_refill import start_token_refiller
    start_token_refiller(app)

    return app
