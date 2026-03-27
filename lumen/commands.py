import click
from flask import current_app
from flask.cli import with_appcontext

from .extensions import db
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_model_access import GroupModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""

    for model_def in yaml_data.get("models", []):
        config = ModelConfig.query.filter_by(model_name=model_def["name"]).first()
        if not config:
            config = ModelConfig(
                model_name=model_def["name"],
                input_cost_per_million=model_def["input_cost_per_million"],
                output_cost_per_million=model_def["output_cost_per_million"],
                active=model_def.get("active", True),
                description=model_def.get("description") or None,
                url=model_def.get("url") or None,
            )
            db.session.add(config)
            db.session.flush()
        else:
            config.input_cost_per_million = model_def["input_cost_per_million"]
            config.output_cost_per_million = model_def["output_cost_per_million"]
            config.active = model_def.get("active", True)
            config.description = model_def.get("description") or None
            config.url = model_def.get("url") or None

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

    # Deactivate ModelConfig rows no longer in yaml and remove their endpoints
    yaml_model_names = {m["name"] for m in yaml_data.get("models", [])}
    for config in ModelConfig.query.all():
        if config.model_name not in yaml_model_names:
            config.active = False
            for ep in list(config.endpoints):
                db.session.delete(ep)

    db.session.commit()


def sync_groups_from_yaml(yaml_data):
    """Upsert Group, GroupLimit, and GroupModelAccess rows from yaml_data['groups'].

    New config format per group (flat keys):
      max, refresh, starting  -> GroupLimit (token pool)
      models: [name, ...]     -> GroupModelAccess (allowed=True for each named model)
      rules: [...]            -> auto-membership rules (handled at login, not here)
    """
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

        # Upsert GroupLimit (token pool)
        if "max" in group_def:
            max_tokens = group_def["max"]
            refresh_tokens = group_def.get("refresh", 0)
            starting_tokens = group_def.get("starting", max_tokens)
            limit = GroupLimit.query.filter_by(group_id=group.id).first()
            if limit:
                limit.max_tokens = max_tokens
                limit.refresh_tokens = refresh_tokens
                limit.starting_tokens = starting_tokens
            else:
                db.session.add(GroupLimit(
                    group_id=group.id,
                    max_tokens=max_tokens,
                    refresh_tokens=refresh_tokens,
                    starting_tokens=starting_tokens,
                ))
        else:
            GroupLimit.query.filter_by(group_id=group.id).delete()

        # Upsert GroupModelAccess (replace all on each sync)
        GroupModelAccess.query.filter_by(group_id=group.id).delete()
        for model_name in group_def.get("models", []):
            mc = ModelConfig.query.filter_by(model_name=model_name).first()
            if mc is None:
                current_app.logger.warning(
                    f"sync_groups_from_yaml: model '{model_name}' not found, skipping access grant for group '{group_name}'"
                )
                continue
            db.session.add(GroupModelAccess(
                group_id=group.id,
                model_config_id=mc.id,
                allowed=True,
            ))

    # Remove config_managed groups no longer in yaml
    for group in Group.query.filter_by(config_managed=True).all():
        if group.name not in yaml_group_names:
            db.session.delete(group)

    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig and ModelEndpoint from models.yaml."""
    sync_models_from_yaml(current_app.config["YAML_DATA"])
    click.echo("Database synced with models from models.yaml.")
