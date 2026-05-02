import click
from flask import current_app
from flask.cli import with_appcontext

from .extensions import db
from lumen.models.global_model_access import GlobalModelAccess
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_model_access import GroupModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint

_VALID_ACCESS_TYPES = {"whitelist", "blacklist", "graylist"}


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
                max_input_tokens=model_def.get("max_input_tokens") or None,
                supports_function_calling=model_def.get("supports_function_calling"),
                input_modalities=model_def.get("input_modalities") or None,
                output_modalities=model_def.get("output_modalities") or None,
                context_window=model_def.get("context_window") or None,
                max_output_tokens=model_def.get("max_output_tokens") or None,
                supports_reasoning=model_def.get("supports_reasoning"),
                knowledge_cutoff=model_def.get("knowledge_cutoff") or None,
                notice=model_def.get("notice") or None,
            )
            db.session.add(config)
            db.session.flush()
        else:
            config.input_cost_per_million = model_def["input_cost_per_million"]
            config.output_cost_per_million = model_def["output_cost_per_million"]
            config.active = model_def.get("active", True)
            config.description = model_def.get("description") or None
            config.url = model_def.get("url") or None
            config.max_input_tokens = model_def.get("max_input_tokens") or None
            config.supports_function_calling = model_def.get("supports_function_calling")
            config.input_modalities = model_def.get("input_modalities") or None
            config.output_modalities = model_def.get("output_modalities") or None
            config.context_window = model_def.get("context_window") or None
            config.max_output_tokens = model_def.get("max_output_tokens") or None
            config.supports_reasoning = model_def.get("supports_reasoning")
            config.knowledge_cutoff = model_def.get("knowledge_cutoff") or None
            config.notice = model_def.get("notice") or None

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

    Config format per group:
      max, refresh, starting    -> GroupLimit (token pool)
      model_access:             -> GroupModelAccess + model_access_default
        default: whitelist      -> group default for unlisted models
        whitelist: [name, ...]
        blacklist: [name, ...]
        graylist:  [name, ...]
      rules: [...]              -> auto-membership rules (handled at login, not here)
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

        if "models" in group_def:
            current_app.logger.warning(
                f"sync_groups_from_yaml: group '{group_name}' uses deprecated 'models:' key; "
                "use 'model_access.whitelist:' instead. The key is ignored."
            )

        # Upsert GroupLimit (coin pool)
        if "max" in group_def:
            max_coins = group_def["max"]
            refresh_coins = group_def.get("refresh", 0)
            starting_coins = group_def.get("starting", max_coins)
            limit = GroupLimit.query.filter_by(group_id=group.id).first()
            if limit:
                limit.max_coins = max_coins
                limit.refresh_coins = refresh_coins
                limit.starting_coins = starting_coins
            else:
                db.session.add(GroupLimit(
                    group_id=group.id,
                    max_coins=max_coins,
                    refresh_coins=refresh_coins,
                    starting_coins=starting_coins,
                ))
        else:
            GroupLimit.query.filter_by(group_id=group.id).delete()

        # Upsert GroupModelAccess from model_access: section
        GroupModelAccess.query.filter_by(group_id=group.id).delete()
        access_cfg = group_def.get("model_access", {})
        group_default = None
        for access_type in _VALID_ACCESS_TYPES:
            for model_name in access_cfg.get(access_type, []):
                if model_name == "*":
                    group_default = access_type
                    continue
                mc = ModelConfig.query.filter_by(model_name=model_name).first()
                if mc is None:
                    current_app.logger.warning(
                        f"sync_groups_from_yaml: model '{model_name}' not found in group '{group_name}' {access_type}, skipping"
                    )
                    continue
                db.session.add(GroupModelAccess(
                    group_id=group.id,
                    model_config_id=mc.id,
                    access_type=access_type,
                ))
        # Explicit default key overrides * shorthand
        if "default" in access_cfg:
            group_default = access_cfg["default"]
        group.model_access_default = group_default

    # Remove config_managed groups no longer in yaml
    for group in Group.query.filter_by(config_managed=True).all():
        if group.name not in yaml_group_names:
            db.session.delete(group)

    db.session.commit()


def sync_global_model_access_from_yaml(yaml_data):
    """Sync GlobalModelAccess from yaml_data['model_access'] and store the default in app config."""
    access_cfg = yaml_data.get("model_access", {})

    global_default = access_cfg.get("default", "whitelist")
    current_app.config["MODEL_ACCESS_DEFAULT"] = global_default

    GlobalModelAccess.query.delete()
    for access_type in _VALID_ACCESS_TYPES:
        for model_name in access_cfg.get(access_type, []):
            if model_name == "*":
                # * is shorthand for default; explicit 'default' key takes precedence
                if "default" not in access_cfg:
                    current_app.config["MODEL_ACCESS_DEFAULT"] = access_type
                    global_default = access_type
                continue
            mc = ModelConfig.query.filter_by(model_name=model_name).first()
            if mc is None:
                current_app.logger.warning(
                    f"sync_global_model_access_from_yaml: model '{model_name}' not found in global {access_type}, skipping"
                )
                continue
            db.session.add(GlobalModelAccess(
                model_config_id=mc.id,
                access_type=access_type,
            ))
    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig, ModelEndpoint, Groups, and GlobalModelAccess from config.yaml."""
    yaml_data = current_app.config["YAML_DATA"]
    sync_models_from_yaml(yaml_data)
    sync_groups_from_yaml(yaml_data)
    sync_global_model_access_from_yaml(yaml_data)
    click.echo("Database synced from config.yaml.")


@click.command("reassign-model")
@click.argument("from_id", type=int)
@click.argument("to_id", type=int)
@with_appcontext
def reassign_model_cmd(from_id, to_id):
    """Move all conversations and stats from one model to another.

    FROM_ID and TO_ID are model_configs.id values.
    """
    from lumen.models.conversation import Conversation
    from lumen.models.model_stat import ModelStat
    from lumen.models.request_log import RequestLog

    src = db.session.get(ModelConfig, from_id)
    dst = db.session.get(ModelConfig, to_id)
    if not src:
        click.echo(f"Error: model_config id {from_id} not found.")
        raise SystemExit(1)
    if not dst:
        click.echo(f"Error: model_config id {to_id} not found.")
        raise SystemExit(1)

    click.echo(f"Reassigning from '{src.model_name}' (id={from_id}) to '{dst.model_name}' (id={to_id})")

    conv_count = Conversation.query.filter_by(model=src.model_name).update({"model": dst.model_name})
    click.echo(f"  conversations updated: {conv_count}")

    # For model_stats, merge rows that might collide on the unique constraint
    existing_dst_stats = {
        (s.entity_id, s.source): s
        for s in ModelStat.query.filter_by(model_config_id=to_id).all()
    }
    src_stats = ModelStat.query.filter_by(model_config_id=from_id).all()
    stats_merged = 0
    stats_moved = 0
    for stat in src_stats:
        key = (stat.entity_id, stat.source)
        if key in existing_dst_stats:
            dst_stat = existing_dst_stats[key]
            dst_stat.requests += stat.requests
            dst_stat.input_tokens += stat.input_tokens
            dst_stat.output_tokens += stat.output_tokens
            dst_stat.cost += stat.cost
            if stat.last_used_at and (not dst_stat.last_used_at or stat.last_used_at > dst_stat.last_used_at):
                dst_stat.last_used_at = stat.last_used_at
            db.session.delete(stat)
            stats_merged += 1
        else:
            stat.model_config_id = to_id
            stats_moved += 1
    click.echo(f"  model_stats moved: {stats_moved}, merged: {stats_merged}")

    log_count = RequestLog.query.filter_by(model_config_id=from_id).update({"model_config_id": to_id})
    click.echo(f"  request_logs updated: {log_count}")

    db.session.commit()
    click.echo("Done.")
