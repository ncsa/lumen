import click
from flask.cli import with_appcontext
from .extensions import db


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""
    from illm.models.model_config import ModelConfig
    from illm.models.model_endpoint import ModelEndpoint

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

        yaml_urls = {ep_def["url"] for ep_def in model_def.get("endpoints", [])}
        for ep in list(config.endpoints):
            if ep.url not in yaml_urls:
                db.session.delete(ep)

        existing_urls = {ep.url for ep in config.endpoints if ep.url in yaml_urls}
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
            else:
                existing_ep = next(e for e in config.endpoints if e.url == ep_def["url"])
                existing_ep.api_key = ep_def["api_key"]
                existing_ep.model_name = ep_def.get("model") or None

    # Deactivate ModelConfig rows no longer in yaml
    yaml_model_names = {m["name"] for m in yaml_data.get("models", [])}
    for config in ModelConfig.query.all():
        if config.model_name not in yaml_model_names:
            config.active = False

    db.session.commit()


def sync_groups_from_yaml(yaml_data):
    """Upsert Group and GroupModelLimit rows from yaml_data['groups']. Must run inside an app context."""
    from illm.models.group import Group
    from illm.models.group_model_limit import GroupModelLimit
    from illm.models.model_config import ModelConfig

    groups_cfg = yaml_data.get("groups", {})
    yaml_group_names = set(groups_cfg.keys())

    for group_name, group_def in groups_cfg.items():
        group = Group.query.filter_by(name=group_name).first()
        if not group:
            group = Group(name=group_name, config_managed=True)
            db.session.add(group)
            db.session.flush()
        else:
            group.config_managed = True

        # Build desired limits: map model_config_id -> (max, refresh, starting)
        desired_limits = {}
        for model_key, limit_def in group_def.items():
            if model_key == "default":
                model_config_id = None
            else:
                mc = ModelConfig.query.filter_by(model_name=model_key).first()
                if mc is None:
                    continue
                model_config_id = mc.id
            max_tokens = limit_def.get("max", 0)
            refresh_tokens = limit_def.get("refresh", 0)
            starting_tokens = limit_def.get("starting", max_tokens)
            desired_limits[model_config_id] = (max_tokens, refresh_tokens, starting_tokens)

        # Upsert GroupModelLimit rows
        existing_limits = {lim.model_config_id: lim for lim in group.limits.all()}
        for model_config_id, (max_t, refresh_t, starting_t) in desired_limits.items():
            if model_config_id in existing_limits:
                lim = existing_limits[model_config_id]
                lim.max_tokens = max_t
                lim.refresh_tokens = refresh_t
                lim.starting_tokens = starting_t
            else:
                db.session.add(GroupModelLimit(
                    group_id=group.id,
                    model_config_id=model_config_id,
                    max_tokens=max_t,
                    refresh_tokens=refresh_t,
                    starting_tokens=starting_t,
                ))

        # Remove limits no longer in yaml (for this group)
        for model_config_id, lim in existing_limits.items():
            if model_config_id not in desired_limits:
                db.session.delete(lim)

    # Remove config_managed groups no longer in yaml
    for group in Group.query.filter_by(config_managed=True).all():
        if group.name not in yaml_group_names:
            db.session.delete(group)

    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig and ModelEndpoint from models.yaml."""
    from flask import current_app
    sync_models_from_yaml(current_app.config["YAML_DATA"])
    click.echo("Database synced with models from models.yaml.")
