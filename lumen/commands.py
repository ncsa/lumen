import os
import shutil
import tempfile

import click
import yaml
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import delete, select, update

from .extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.models.group_model_access import GroupModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint

# Maps config-input access vocabulary (new + legacy) to the stored value.
# Acknowledgement (graylist) is a model-level property now, so legacy 'graylist'
# at a scope only sets access to 'allowed'.
_ACCESS_INPUT = {
    "allowed": "allowed",
    "blocked": "blocked",
    "whitelist": "allowed",
    "blacklist": "blocked",
    "graylist": "allowed",
}
_LEGACY_ACCESS_TERMS = {"whitelist", "blacklist", "graylist"}
# Recognized model_access list keys at a scope (group/client), new + legacy.
_SCOPE_ACCESS_KEYS = ("allowed", "blocked", "whitelist", "blacklist", "graylist")

# Deduplicate deprecation warnings; the config watcher re-runs sync every 5s.
_warned: set = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        current_app.logger.warning(msg, *args)


def write_config_yaml(config_path, data):
    """Write ``data`` (a top-level config dict) to config_path as YAML.

    Each top-level key is dumped as its own document fragment (preserving key order
    and keeping blocks visually separated), the previous file is backed up to
    ``<config>.bak``, and the new content is written atomically via a temp file.
    Shared by the admin config editor and client creation.
    """
    parts = [
        yaml.dump({k: v}, Dumper=yaml.SafeDumper, default_flow_style=False, allow_unicode=True, sort_keys=False)
        for k, v in data.items()
    ]
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(parts))
        # Back up the current config before overwriting so a partial or
        # malformed save can be recovered from <config>.bak.
        if os.path.exists(config_path):
            shutil.copy2(config_path, config_path + ".bak")
        shutil.copyfile(tmp_path, config_path)
    finally:
        os.unlink(tmp_path)


def backfill_clients_to_config(yaml_data, config_path):
    """Ensure every client entity in the DB has an entry in config.yaml.

    Existing installs have clients that live only in the DB (created before client
    creation wrote them to config); add an empty entry for each one missing from the
    file so it reflects which clients exist. Mutates yaml_data in place and writes the
    file only when something was added. Returns True if the file was written.
    """
    names = db.session.execute(
        select(Entity.name).where(Entity.entity_type == "client")
    ).scalars().all()
    clients_cfg = yaml_data.setdefault("clients", {})
    added = False
    for name in names:
        if name not in clients_cfg:
            clients_cfg[name] = {}
            added = True
    if added:
        write_config_yaml(config_path, yaml_data)
    return added


def _normalize_access(value, *, context=""):
    """Map config access vocabulary to a stored value ('allowed' or 'blocked').

    Accepts the new allowed/blocked terms and the legacy whitelist/blacklist/graylist
    (with a deprecation warning). Returns None for unknown values.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in _LEGACY_ACCESS_TERMS:
        if v == "graylist":
            _warn_once(("graylist", context),
                       "deprecated access term 'graylist'%s; acknowledgement is now a model "
                       "property — set 'needs_ack: true' on the model instead", _ctx(context))
        else:
            _warn_once((v, context), "deprecated access term '%s'%s; use allowed/blocked", v, _ctx(context))
    out = _ACCESS_INPUT.get(v)
    if out is None:
        _warn_once(("unknown", v, context), "unknown access value '%s'%s; ignoring", v, _ctx(context))
    return out


def _ctx(context):
    return f" in {context}" if context else ""


def _token_fields(cfg):
    """Return (max, refresh, starting) filled from cfg + global TOKEN_DEFAULTS.

    Returns None when the config block specifies no token fields at all, so the
    caller drops the limit row and the entity falls through to the global pool.
    """
    if not ({"max", "refresh", "starting"} & cfg.keys()):
        return None
    td = current_app.config.get("TOKEN_DEFAULTS", {"max": 0, "refresh": 0, "starting": 0})
    max_coins = cfg.get("max", td["max"])
    refresh_coins = cfg.get("refresh", td["refresh"])
    starting_coins = cfg.get("starting", cfg.get("max", td["starting"]))
    return max_coins, refresh_coins, starting_coins


def _parse_scope_access(access_cfg, context):
    """Parse a model_access block.

    Returns (pairs, default, ack_models):
      pairs       – [(model_name, 'allowed'|'blocked'), ...]
      default     – scope default ('allowed'/'blocked') from a 'default:' key or '*' shorthand
      ack_models  – model names listed under a legacy 'graylist:' key. Acknowledgement is now a
                    model property, so callers set needs_ack=True on these to preserve the old
                    "graylisted model requires consent" behavior when loading a v1 config.
    """
    default = None
    pairs = []
    ack_models = []
    for key in _SCOPE_ACCESS_KEYS:
        if key not in access_cfg:
            continue  # don't warn about a legacy key the config doesn't actually use
        stored = _normalize_access(key, context=context)
        if stored is None:
            continue
        for model_name in access_cfg.get(key, []) or []:
            if model_name == "*":
                default = stored
                continue
            pairs.append((model_name, stored))
            if key == "graylist":
                ack_models.append(model_name)
    if "default" in access_cfg:
        default = _normalize_access(access_cfg["default"], context=context)
    return pairs, default, ack_models


def _apply_legacy_ack(ack_models, models_by_name):
    """Set needs_ack=True on models that a legacy scope 'graylist:' list referenced.

    Acknowledgement moved from a per-scope concept to a model property, so a v1 config's
    graylist list must keep its models requiring consent. Only ever turns needs_ack ON
    (a v2 config has no graylist key, so this is a no-op there)."""
    for name in ack_models:
        mc = models_by_name.get(name)
        if mc is not None and not mc.needs_ack:
            mc.needs_ack = True


def _apply_model_fields(config, model_def):
    config.input_cost_per_million = model_def["input_cost_per_million"]
    config.output_cost_per_million = model_def["output_cost_per_million"]
    if "audio_cost_per_hour" in model_def:
        config.audio_cost_per_hour = model_def["audio_cost_per_hour"]
    elif "audio_cost_per_minute" in model_def:
        # Legacy per-minute pricing -> per-hour (×60).
        legacy = model_def.get("audio_cost_per_minute")
        _warn_once(("audio-cost", model_def.get("name")),
                   "deprecated 'audio_cost_per_minute' on model '%s'; use 'audio_cost_per_hour' instead", model_def.get("name"))
        config.audio_cost_per_hour = (legacy * 60) if legacy is not None else None
    else:
        config.audio_cost_per_hour = None
    _apply_model_access(config, model_def)
    config.description = model_def.get("description") or None
    config.url = model_def.get("url") or None
    config.supports_function_calling = model_def.get("supports_function_calling")
    config.input_modalities = model_def.get("input_modalities") or None
    config.output_modalities = model_def.get("output_modalities") or None
    config.context_window = model_def.get("context_window") or None
    config.max_output_tokens = model_def.get("max_output_tokens") or None
    config.supports_reasoning = model_def.get("supports_reasoning")
    config.knowledge_cutoff = model_def.get("knowledge_cutoff") or None
    config.notice = model_def.get("notice") or None
    config.ack_message = model_def.get("ack_message") or None


def _apply_model_access(config, model_def):
    """Set config.access / needs_ack / disabled from a model definition.

    Honors the new orthogonal fields and bridges the legacy `active:` boolean
    (active: false -> disabled) with a deprecation warning.
    """
    config.needs_ack = bool(model_def.get("needs_ack", False))

    disabled = model_def.get("disabled")
    access = model_def.get("access")
    if disabled is None and access is None and model_def.get("active") is False:
        _warn_once(("active", model_def.get("name")),
                   "deprecated 'active: false' on model '%s'; use 'disabled: true' instead", model_def.get("name"))
        disabled = True
    config.disabled = bool(disabled)

    if access is None:
        config.access = None  # inherit group/global defaults
    else:
        a = str(access).strip().lower()
        if a not in ("allowed", "blocked"):
            _warn_once(("model-access", a, model_def.get("name")),
                       "invalid model access '%s' on model '%s'; ignoring (will inherit defaults)", a, model_def.get("name"))
            a = None
        config.access = a


def _reconcile_endpoints(config, model_def):
    yaml_urls = {ep_def["url"] for ep_def in model_def.get("endpoints", [])}
    # Build dict once to avoid O(n²) iteration over the endpoints collection
    existing_by_url = {ep.url: ep for ep in config.endpoints}

    for ep in list(existing_by_url.values()):
        if ep.url not in yaml_urls:
            db.session.delete(ep)

    for ep_def in model_def.get("endpoints", []):
        if ep_def["url"] not in existing_by_url:
            db.session.add(ModelEndpoint(
                model_config_id=config.id,
                url=ep_def["url"],
                api_key=ep_def["api_key"],
                model_name=ep_def.get("model") or None,
                healthy=False,
            ))
        else:
            existing_ep = existing_by_url[ep_def["url"]]
            existing_ep.api_key = ep_def["api_key"]
            existing_ep.model_name = ep_def.get("model") or None


def _deactivate_removed_models(yaml_model_names):
    stmt = select(ModelConfig)
    if yaml_model_names:
        stmt = stmt.where(ModelConfig.model_name.notin_(yaml_model_names))
    deactivated_ids = [c.id for c in db.session.execute(stmt).scalars().all()]
    if not deactivated_ids:
        return
    db.session.execute(
        delete(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(deactivated_ids))
    )
    db.session.execute(
        update(ModelConfig).where(ModelConfig.id.in_(deactivated_ids)).values(disabled=True)
    )
    db.session.expire_all()


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""
    for model_def in yaml_data.get("models", []):
        config = db.session.execute(select(ModelConfig).filter_by(model_name=model_def["name"])).scalar_one_or_none()
        if not config:
            config = ModelConfig(model_name=model_def["name"])
            db.session.add(config)
        _apply_model_fields(config, model_def)
        db.session.flush()  # ensure config.id exists before reconciling endpoints
        _reconcile_endpoints(config, model_def)

    _deactivate_removed_models({m["name"] for m in yaml_data.get("models", [])})
    db.session.commit()


def sync_groups_from_yaml(yaml_data):
    """Upsert Group, GroupLimit, and GroupModelAccess rows from yaml_data['groups'].

    Config format per group:
      max, refresh, starting    -> GroupLimit (token pool); missing fields fall back to defaults.tokens
      model_access:             -> GroupModelAccess + model_access_default
        default: allowed        -> group default for unlisted models
        allowed: [name, ...]
        blocked: [name, ...]
      rules: [...]              -> auto-membership rules (handled at login, not here)
    """
    groups_cfg = yaml_data.get("groups", {})
    yaml_group_names = set(groups_cfg.keys())

    # Preload all models once to avoid an N+1 lookup per model_access entry
    models_by_name = {
        mc.model_name: mc for mc in db.session.execute(select(ModelConfig)).scalars().all()
    }

    for group_name, group_def in groups_cfg.items():
        group = db.session.execute(select(Group).filter_by(name=group_name)).scalar_one_or_none()
        if not group:
            group = Group(name=group_name, config_managed=True)
            db.session.add(group)
            db.session.flush()
        else:
            group.config_managed = True

        if "models" in group_def:
            current_app.logger.warning(
                "sync_groups_from_yaml: group '%s' uses deprecated 'models:' key; "
                "use 'model_access.allowed:' instead. The key is ignored.",
                group_name,
            )

        # Upsert GroupLimit (coin pool)
        pool = _token_fields(group_def)
        if pool is not None:
            max_coins, refresh_coins, starting_coins = pool
            limit = db.session.execute(select(GroupLimit).filter_by(group_id=group.id)).scalar_one_or_none()
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
            db.session.execute(delete(GroupLimit).where(GroupLimit.group_id == group.id))

        # Upsert GroupModelAccess from model_access: section
        db.session.execute(delete(GroupModelAccess).where(GroupModelAccess.group_id == group.id))
        access_cfg = group_def.get("model_access", {})
        pairs, group_default, ack_models = _parse_scope_access(access_cfg, context=f"group '{group_name}'")
        _apply_legacy_ack(ack_models, models_by_name)
        for model_name, access_type in pairs:
            mc = models_by_name.get(model_name)
            if mc is None:
                current_app.logger.warning(
                    "sync_groups_from_yaml: model '%s' not found in group '%s', skipping",
                    model_name, group_name,
                )
                continue
            db.session.add(GroupModelAccess(
                group_id=group.id,
                model_config_id=mc.id,
                access_type=access_type,
            ))
        group.model_access_default = group_default

    # Remove config_managed groups no longer in yaml
    for group in db.session.execute(select(Group).filter_by(config_managed=True)).scalars().all():
        if group.name not in yaml_group_names:
            db.session.delete(group)

    db.session.commit()


def _desired_groups_from_config(email, yaml_data):
    """Return group names from users.default.groups and users.<email>.groups."""
    users_cfg = yaml_data.get("users", {})
    names = ["default"]
    for name in users_cfg.get("default", {}).get("groups", []):
        if name not in names:
            names.append(name)
    for name in users_cfg.get(email, {}).get("groups", []):
        if name not in names:
            names.append(name)
    return names


def sync_user_groups_from_yaml(yaml_data):
    """Reconcile user memberships in non-auto (explicitly assigned) groups from yaml.

    Non-auto groups are those without a `rules:` key — they are assigned via
    users.default.groups / users.<email>.groups and need no CILogon userinfo, so they can
    be reconciled at config-reload time. Rule-based ("auto") group memberships depend on
    login-time userinfo and are left untouched: this function only adds/removes memberships
    whose group is in the non-auto set, so auto memberships are never deleted here.
    """
    groups_cfg = yaml_data.get("groups", {})
    non_auto_names = {name for name, gdef in groups_cfg.items() if not (gdef or {}).get("rules")}
    non_auto_names.add("default")

    groups_by_name = {
        g.name: g
        for g in db.session.execute(select(Group).where(Group.name.in_(non_auto_names))).scalars().all()
    }
    non_auto_ids = {g.id for g in groups_by_name.values()}

    for entity in db.session.execute(
        select(Entity).filter_by(entity_type="user").where(Entity.email.isnot(None))
    ).scalars().all():
        desired_ids = {
            groups_by_name[name].id
            for name in _desired_groups_from_config(entity.email, yaml_data)
            if name in groups_by_name
        }
        existing_by_group = {
            m.group_id: m
            for m in db.session.execute(select(GroupMember).filter_by(entity_id=entity.id)).scalars().all()
        }
        for group_id, member in existing_by_group.items():
            if group_id in non_auto_ids and member.config_managed and group_id not in desired_ids:
                db.session.delete(member)
        for group_id in desired_ids:
            if group_id not in existing_by_group:
                db.session.add(GroupMember(group_id=group_id, entity_id=entity.id, config_managed=True))

    db.session.commit()


def sync_clients_from_yaml(yaml_data):
    """Sync EntityLimit and EntityModelAccess for client (service) entities from yaml_data['clients'].

    Config format:
      clients:
        default:                    <- applied to all clients without a named entry
          max: 100
          refresh: 0
          starting: 100
          model_access:
            default: allowed        <- entity-level default for unlisted models
            allowed: [name, ...]
            blocked: [name, ...]
        my-client-name:             <- overrides for a specific client
          max: 500
          groups: [research]        <- group memberships granting extra model access
    """
    clients_cfg = yaml_data.get("clients", {})
    if not clients_cfg:
        return

    default_cfg = clients_cfg.get("default", {})
    named_cfg = {k: v for k, v in clients_cfg.items() if k != "default"}

    client_entities = db.session.execute(select(Entity).filter_by(entity_type="client")).scalars().all()

    # Preload all models once to avoid an N+1 lookup per model_access entry
    models_by_name = {
        mc.model_name: mc for mc in db.session.execute(select(ModelConfig)).scalars().all()
    }
    # Preload groups by name for membership reconciliation.
    groups_by_name = {
        g.name: g for g in db.session.execute(select(Group)).scalars().all()
    }

    for entity in client_entities:
        # An empty named entry (e.g. `my-client: {}`, written on client creation so the
        # file records that the client exists) carries no settings, so fall back to the
        # shared `default` block just as a client with no entry at all would.
        cfg = named_cfg.get(entity.name) or default_cfg
        if not cfg:
            continue

        # Upsert EntityLimit
        pool = _token_fields(cfg)
        if pool is not None:
            max_coins, refresh_coins, starting_coins = pool
            limit = db.session.execute(select(EntityLimit).filter_by(entity_id=entity.id)).scalar_one_or_none()
            if limit:
                limit.max_coins = max_coins
                limit.refresh_coins = refresh_coins
                limit.starting_coins = starting_coins
                limit.config_managed = True
            else:
                db.session.add(EntityLimit(
                    entity_id=entity.id,
                    max_coins=max_coins,
                    refresh_coins=refresh_coins,
                    starting_coins=starting_coins,
                    config_managed=True,
                ))
        else:
            db.session.execute(delete(EntityLimit).where(EntityLimit.entity_id == entity.id, EntityLimit.config_managed == True))

        # Sync model_access
        access_cfg = cfg.get("model_access", {})
        pairs, entity_default, ack_models = _parse_scope_access(access_cfg, context=f"client '{entity.name}'")
        _apply_legacy_ack(ack_models, models_by_name)
        entity.model_access_default = entity_default

        db.session.execute(delete(EntityModelAccess).where(EntityModelAccess.entity_id == entity.id))
        for model_name, access_type in pairs:
            mc = models_by_name.get(model_name)
            if mc is None:
                current_app.logger.warning(
                    "sync_clients_from_yaml: model '%s' not found for client '%s', skipping",
                    model_name, entity.name,
                )
                continue
            db.session.add(EntityModelAccess(
                entity_id=entity.id,
                model_config_id=mc.id,
                access_type=access_type,
            ))

        # Sync config-managed group memberships. Membership can grant model access the
        # client's own rules would otherwise block (resolved in services/llm.py).
        desired_group_ids = set()
        for gname in cfg.get("groups", []) or []:
            group = groups_by_name.get(gname)
            if group is None:
                current_app.logger.warning(
                    "sync_clients_from_yaml: group '%s' not found for client '%s', skipping",
                    gname, entity.name,
                )
                continue
            desired_group_ids.add(group.id)

        existing_by_group = {
            m.group_id: m
            for m in db.session.execute(select(GroupMember).filter_by(entity_id=entity.id)).scalars().all()
        }
        for group_id, member in existing_by_group.items():
            if member.config_managed and group_id not in desired_group_ids:
                db.session.delete(member)
        for group_id in desired_group_ids:
            if group_id not in existing_by_group:
                db.session.add(GroupMember(group_id=group_id, entity_id=entity.id, config_managed=True))

    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig, ModelEndpoint, Groups, and clients from config.yaml."""
    yaml_data = current_app.config["YAML_DATA"]
    sync_models_from_yaml(yaml_data)
    sync_groups_from_yaml(yaml_data)
    sync_clients_from_yaml(yaml_data)
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

    result = db.session.execute(update(Conversation).where(Conversation.model == src.model_name).values(model=dst.model_name))
    conv_count = result.rowcount
    click.echo(f"  conversations updated: {conv_count}")

    # For model_stats, merge rows that might collide on the unique constraint
    existing_dst_stats = {
        (s.entity_id, s.source): s
        for s in db.session.execute(select(ModelStat).filter_by(model_config_id=to_id)).scalars().all()
    }
    src_stats = db.session.execute(select(ModelStat).filter_by(model_config_id=from_id)).scalars().all()
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

    result = db.session.execute(update(RequestLog).where(RequestLog.model_config_id == from_id).values(model_config_id=to_id))
    log_count = result.rowcount
    click.echo(f"  request_logs updated: {log_count}")

    db.session.commit()
    click.echo("Done.")
