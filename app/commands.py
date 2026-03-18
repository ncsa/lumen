import click
from flask.cli import with_appcontext
from .extensions import db


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""
    from app.models.model_config import ModelConfig
    from app.models.model_endpoint import ModelEndpoint

    for model_def in yaml_data.get("models", []):
        config = ModelConfig.query.filter_by(model_name=model_def["name"]).first()
        if not config:
            config = ModelConfig(
                model_name=model_def["name"],
                input_cost_per_million=model_def["input_cost_per_million"],
                output_cost_per_million=model_def["output_cost_per_million"],
                active=model_def.get("active", True),
            )
            db.session.add(config)
            db.session.flush()
        else:
            config.input_cost_per_million = model_def["input_cost_per_million"]
            config.output_cost_per_million = model_def["output_cost_per_million"]
            config.active = model_def.get("active", True)

        existing_urls = {ep.url for ep in config.endpoints}
        for ep_def in model_def.get("endpoints", []):
            if ep_def["url"] not in existing_urls:
                ep = ModelEndpoint(
                    model_config_id=config.id,
                    url=ep_def["url"],
                    api_key=ep_def["api_key"],
                    model_name=ep_def.get("model") or None,
                    healthy=False,
                )
                db.session.add(ep)
                existing_urls.add(ep_def["url"])

    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig and ModelEndpoint from models.yaml."""
    from flask import current_app
    sync_models_from_yaml(current_app.config["YAML_DATA"])
    click.echo("Database synced with models from models.yaml.")
