import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import click
import yaml
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.models.group_model_access import GroupModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.timeutils import utcnow

from .extensions import db

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
# Recognized model_access list keys at a scope (group/project), new + legacy.
_SCOPE_ACCESS_KEYS = ("allowed", "blocked", "whitelist", "blacklist", "graylist")

# The entity-dimensioned continuous aggregate the per-entity /usage queries read.
# `enable-retention` refuses while this is empty: dropping raw chunks then destroys
# history that exists in no aggregate.
ENTITY_AGGREGATE = "request_counts_hourly_by_entity"

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
    Shared by the admin config editor and project creation.

    If you add a new secret-bearing key to the schema, add it to
    :data:`SENSITIVE_KEYS` in :mod:`lumen.services.config_watcher` so it is
    masked before being sent to the admin browser (see ``config_api_get``).
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


def backfill_projects_to_config(yaml_data, config_path):
    """Ensure every project entity in the DB has an entry in config.yaml.

    Existing installs have projects that live only in the DB (created before project
    creation wrote them to config); add an empty entry for each one missing from the
    file so it reflects which projects exist. Mutates yaml_data in place and writes the
    file only when something was added. Returns True if the file was written.
    """
    names = db.session.execute(
        select(Entity.name).where(Entity.entity_type == "project")
    ).scalars().all()
    projects_cfg = yaml_data.setdefault("projects", {})
    added = False
    for name in names:
        if name not in projects_cfg:
            projects_cfg[name] = {}
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


def _normalize_model_url(url):
    """Expand a bare HuggingFace repo id (`org/name`) to a full huggingface.co URL.

    Common HuggingFace host variants (huggingface.com, www.) are rewritten to
    huggingface.co so the README lookup recognizes them; any other URL is used
    as given."""
    if not url:
        return None
    if "://" not in url and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        return f"https://huggingface.co/{url}"
    parsed = urlparse(url)
    if parsed.netloc.lower() in ("huggingface.com", "www.huggingface.co", "www.huggingface.com"):
        url = parsed._replace(netloc="huggingface.co").geturl()
    return url


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
    config.url = _normalize_model_url(model_def.get("url"))
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


def sync_projects_from_yaml(yaml_data):
    """Sync EntityLimit and EntityModelAccess for project (service) entities from yaml_data['projects'].

    Config format:
      projects:
        default:                    <- applied to all projects without a named entry
          max: 100
          refresh: 0
          starting: 100
          model_access:
            default: allowed        <- entity-level default for unlisted models
            allowed: [name, ...]
            blocked: [name, ...]
        my-project-name:            <- overrides for a specific project
          max: 500
          groups: [research]        <- group memberships granting extra model access
    """
    projects_cfg = yaml_data.get("projects", {})
    if not projects_cfg:
        return

    default_cfg = projects_cfg.get("default", {})
    named_cfg = {k: v for k, v in projects_cfg.items() if k != "default"}

    project_entities = db.session.execute(select(Entity).filter_by(entity_type="project")).scalars().all()

    # Preload all models once to avoid an N+1 lookup per model_access entry
    models_by_name = {
        mc.model_name: mc for mc in db.session.execute(select(ModelConfig)).scalars().all()
    }
    # Preload groups by name for membership reconciliation.
    groups_by_name = {
        g.name: g for g in db.session.execute(select(Group)).scalars().all()
    }

    for entity in project_entities:
        # An empty named entry (e.g. `my-project: {}`, written on project creation so the
        # file records that the project exists) carries no settings, so fall back to the
        # shared `default` block just as a project with no entry at all would.
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
            db.session.execute(delete(EntityLimit).where(EntityLimit.entity_id == entity.id, EntityLimit.config_managed == True))  # noqa: E712 — SQL comparison, not a truth check

        # Sync model_access
        access_cfg = cfg.get("model_access", {})
        pairs, entity_default, ack_models = _parse_scope_access(access_cfg, context=f"project '{entity.name}'")
        _apply_legacy_ack(ack_models, models_by_name)
        entity.model_access_default = entity_default

        db.session.execute(delete(EntityModelAccess).where(EntityModelAccess.entity_id == entity.id))
        for model_name, access_type in pairs:
            mc = models_by_name.get(model_name)
            if mc is None:
                current_app.logger.warning(
                    "sync_projects_from_yaml: model '%s' not found for project '%s', skipping",
                    model_name, entity.name,
                )
                continue
            db.session.add(EntityModelAccess(
                entity_id=entity.id,
                model_config_id=mc.id,
                access_type=access_type,
            ))

        # Sync config-managed group memberships. Membership can grant model access the
        # project's own rules would otherwise block (resolved in services/llm.py).
        desired_group_ids = set()
        for gname in cfg.get("groups", []) or []:
            group = groups_by_name.get(gname)
            if group is None:
                current_app.logger.warning(
                    "sync_projects_from_yaml: group '%s' not found for project '%s', skipping",
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


def sync_user_limits_from_yaml(yaml_data):
    """Sync per-user EntityLimit (coin pool) rows from yaml_data['users'].

    Mirrors the EntityLimit upsert in sync_projects_from_yaml, but for user entities.
    Unlike the login path (_apply_user_model_overrides in auth/routes.py), this runs on
    every config reload so admin edits to a user's max/refresh/starting take effect
    immediately instead of waiting for the user to log in again.

    The live coin balance (EntityBalance.coins_left) is reset to the new starting value
    only when an EXISTING per-user limit row's starting_coins actually changes. Adding a
    first per-user block for a user on the global pool preserves their accrued balance;
    changing only max/refresh leaves the balance untouched.

    The upsert overwrites unconditionally and forces config_managed=True, diverging from
    the login path's `if limit and limit.config_managed` guard (auth/routes.py:76). No
    endpoint creates non-config-managed user EntityLimit rows today; a future manual-limit
    feature would need to reconcile this.
    """
    users_cfg = yaml_data.get("users", {}) or {}
    for email, cfg in users_cfg.items():
        # A null/non-dict entry behaves like an empty block (no pool → fall through to
        # the global pool), matching the login path where user_cfg defaults to {}.
        if not isinstance(cfg, dict):
            cfg = {}
        entity = db.session.execute(
            select(Entity).filter_by(entity_type="user", email=email)
        ).scalar_one_or_none()
        if entity is None:
            # The literal "default" key and any not-yet-logged-in users resolve to no
            # Entity; their limit row is created at login. Skip.
            continue

        # Unwrap the pool exactly as the login path does (auth/routes.py:71-72): a nested
        # `pool:` block and the flat top-level form are both valid.
        pool_src = cfg.get("pool") or cfg
        pool = _token_fields(pool_src) if isinstance(pool_src, dict) else None

        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=entity.id)
        ).scalar_one_or_none()

        if pool is None:
            # No token fields in this user's block: drop any config-managed limit so the
            # entity falls through to the group/global pool.
            if limit is not None and limit.config_managed:
                db.session.delete(limit)
            continue

        max_coins, refresh_coins, starting_coins = pool

        # Capture the prior starting value BEFORE the in-place mutation below — the
        # project-sync pattern mutates the row in place, so reading it after would always
        # yield the new value and the balance-reset check would never fire.
        old_starting = float(limit.starting_coins) if limit is not None else None

        if limit is not None:
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

        # Reset the live balance only on a genuine starting change of an existing
        # per-user limit. old_starting is None exactly when there was no prior limit row
        # (first per-user block) — preserve the accrued balance in that case.
        # -2 == unlimited; skip the reset (matches reset_user_tokens, admin/routes.py:78).
        starting_changed = (old_starting is not None) and (old_starting != float(starting_coins))
        if starting_changed and max_coins != -2:
            balance = db.session.execute(
                select(EntityBalance).filter_by(entity_id=entity.id)
            ).scalar_one_or_none()
            if balance is not None:
                balance.coins_left = starting_coins
                balance.last_refill_at = utcnow()

    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig, ModelEndpoint, Groups, and projects from config.yaml."""
    yaml_data = current_app.config["YAML_DATA"]
    sync_models_from_yaml(yaml_data)
    sync_groups_from_yaml(yaml_data)
    sync_projects_from_yaml(yaml_data)
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


# --------------------------------------------------------------------------- #
# TimescaleDB lifecycle (Phase 8): aggregate backfill and retention.           #
# --------------------------------------------------------------------------- #

def _autocommit_connection():
    """Open a connection that is *not* inside a transaction block.

    ``CALL refresh_continuous_aggregate`` cannot run inside a transaction, and a
    ``flask`` command using ``db.session`` is already in one — the CALL then fails
    with "cannot run inside a transaction block" in the middle of an operator's
    maintenance window. Same technique as ``seed_analytics.py``.
    """
    return db.engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _continuous_aggregates(conn):
    """Names of every continuous aggregate in this database, alphabetically."""
    return conn.execute(text(
        "SELECT view_name FROM timescaledb_information.continuous_aggregates ORDER BY view_name"
    )).scalars().all()


def _bucket_column(conn, view_name):
    """Name of the aggregate's ``time_bucket`` column, which is always its first."""
    return conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :v AND ordinal_position = 1"
    ), {"v": view_name}).scalar()


def _materialization_hypertable(conn, view_name):
    """The physical table holding an aggregate's *materialised* rows.

    Not the view. With ``materialized_only = false`` the view unions materialised rows
    with a live scan of the raw rows above the watermark, so ``MIN(bucket)`` read through
    the view reports how far back *raw* goes for as long as the watermark is still at
    -infinity — which is exactly the freshly-migrated state where no backfill has run.
    Only the materialisation hypertable answers what would still be there once retention
    has dropped the raw chunks.
    """
    return conn.execute(text(
        "SELECT materialization_hypertable_schema || '.' || materialization_hypertable_name "
        "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
    ), {"v": view_name}).scalar()


def _retention_drop_after(conn):
    """The retention policy's interval on request_logs, or None if retention is off."""
    return conn.execute(text(
        "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
    )).scalar()


def _fmt(dt):
    """Format a timestamp for the terminal. Always UTC — this is an operator tool."""
    if dt is None:
        return "none"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _next_month(dt):
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=1)


# TimescaleDB refuses an *overlapping* refresh outright instead of waiting for it: while
# a background continuous-aggregate policy job is mid-refresh, the CALL raises SQLSTATE
# 55P03 (lock_not_available) immediately -- measured at 0.02s on 2.27.2. request_metrics_1m
# has a policy firing every minute and the entity aggregate one every hour, so a 13-month
# month-by-month backfill will collide at least once. Without a retry that aborts the whole
# command partway and leaves the operator with a half-backfilled aggregate.
_LOCK_NOT_AVAILABLE = "55P03"
_REFRESH_RETRY_BUDGET = 60.0
_REFRESH_RETRY_SLEEP = 2.0


def _refresh_window(conn, name, start, end):
    """Refresh one window, waiting out a concurrent refresh. Returns the retry count.

    Matches the SQLSTATE rather than the message text: the message is not part of any
    stability guarantee, and matching it would turn a version bump into a silent loss
    of the retry.
    """
    deadline = time.monotonic() + _REFRESH_RETRY_BUDGET
    retries = 0
    while True:
        try:
            conn.execute(text("CALL refresh_continuous_aggregate(:n, :s, :e)"),
                         {"n": name, "s": start, "e": end})
            return retries
        except DBAPIError as exc:
            if getattr(exc.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            if time.monotonic() + _REFRESH_RETRY_SLEEP > deadline:
                raise
            retries += 1
            time.sleep(_REFRESH_RETRY_SLEEP)


@click.command("backfill-aggregate")
@click.option("--name", default=ENTITY_AGGREGATE, show_default=True,
              help="Continuous aggregate to materialise.")
@click.option("--from", "from_month", metavar="YYYY-MM", default=None,
              help="First month to refresh. Defaults to the month of the oldest request_logs row.")
@click.option("--force", is_flag=True,
              help="Refresh months starting before the retention boundary anyway. Read the refusal first.")
@with_appcontext
def backfill_aggregate_cmd(name, from_month, force):
    """Materialise a continuous aggregate's full history, month by month.

    A policy-created aggregate holds only its ``start_offset`` window, so per-user
    "All Time" charts stay near-empty until this is run. One
    ``CALL refresh_continuous_aggregate(NULL, NULL)`` over 13 months of a production
    hypertable is a single long transaction with unbounded memory, so this walks
    month by month, oldest first, and prints the row counts either side of each call.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"backfill-aggregate requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    with _autocommit_connection() as conn:
        if name not in _continuous_aggregates(conn):
            click.echo(f"Error: '{name}' is not a continuous aggregate in this database.")
            raise SystemExit(1)
        bucket = _bucket_column(conn, name)

        if from_month:
            try:
                start = datetime.strptime(from_month, "%Y-%m").replace(tzinfo=timezone.utc)
            except ValueError:
                click.echo(f"Error: --from must be YYYY-MM, got '{from_month}'.")
                raise SystemExit(1)
        else:
            start = conn.execute(text(
                "SELECT date_trunc('month', MIN(time) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                "FROM request_logs"
            )).scalar()
            if start is None:
                click.echo("request_logs is empty; nothing to backfill.")
                return

        # Contract (d): refreshing a window whose raw chunks were dropped recomputes it
        # as EMPTY and DELETES the materialised rows, with no error at all. A backfill
        # run a year after retention was enabled would erase every user's pre-retention
        # history in one call, which is strictly worse than the truncation the whole
        # phase exists to avoid — so refuse rather than trust the operator's arithmetic.
        drop_after = _retention_drop_after(conn)
        if drop_after is not None:
            boundary = conn.execute(text("SELECT now() - CAST(:d AS interval)"),
                                    {"d": drop_after}).scalar().astimezone(timezone.utc)
            if start < boundary:
                erased = conn.execute(text(
                    f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :b'
                ), {"s": start, "b": boundary}).scalar()
                if not force:
                    # The boundary's own month still starts before the boundary, so the
                    # hint has to name the month after it or the re-run is refused too.
                    safe = _next_month(boundary.replace(day=1, hour=0, minute=0,
                                                        second=0, microsecond=0))
                    click.echo(
                        f"Error: refusing to refresh '{name}' from {start:%Y-%m}.\n"
                        f"  retention on request_logs is '{drop_after}', so raw chunks before\n"
                        f"  {_fmt(boundary)} may already have been dropped, and every one that\n"
                        f"  has not will be. Refreshing that window recomputes it as\n"
                        f"  EMPTY and DELETES the materialised rows, with no error: {erased} rows\n"
                        f"  of '{name}' would be erased.\n"
                        f"  Re-run with --from {safe:%Y-%m} to stay inside retention, or --force."
                    )
                    raise SystemExit(1)
                click.echo(
                    f"WARNING: --force given; months before {_fmt(boundary)} are outside retention "
                    f"and their {erased} materialised rows will be erased."
                )

        now = datetime.now(timezone.utc)
        click.echo(f"Backfilling '{name}' from {start:%Y-%m} to {now:%Y-%m}, month by month.")
        refreshed = []
        month = start
        while month <= now:
            nxt = _next_month(month)
            # Never refresh past "now". A window ending in the future materialises the
            # current bucket and moves the aggregate's watermark beyond it, which turns
            # real-time aggregation OFF for that bucket: every request logged after the
            # backfill stays invisible on /usage until a scheduled refresh catches up.
            # Measured on 2.27.2 — with the month end (future) the next insert was
            # missing; with now() the watermark stayed two buckets back and it showed.
            end = min(nxt, now)
            raw = conn.execute(text(
                "SELECT COUNT(*) FROM request_logs WHERE time >= :s AND time < :e"
            ), {"s": month, "e": nxt}).scalar()
            click.echo(f"  {month:%Y-%m}: {raw} request_logs rows ... ", nl=False)
            try:
                retries = _refresh_window(conn, name, month, end)
            except DBAPIError as exc:
                click.echo("FAILED")
                click.echo(f"Error: refreshing {month:%Y-%m} failed: {exc.orig}")
                if refreshed:
                    click.echo(f"  Refreshed OK: {refreshed[0]} .. {refreshed[-1]} "
                               f"({len(refreshed)} months).")
                else:
                    click.echo("  No month was refreshed.")
                click.echo(f"  Resume with: flask backfill-aggregate "
                           f"--name {name} --from {month:%Y-%m}")
                raise SystemExit(1)
            aggregated = conn.execute(text(
                f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :e'
            ), {"s": month, "e": nxt}).scalar()
            click.echo(f"{aggregated} aggregate rows OK"
                       + (f" (after {retries} lock retries)" if retries else ""))
            refreshed.append(f"{month:%Y-%m}")
            month = nxt

    if refreshed:
        click.echo(f"Backfill complete: {len(refreshed)} months refreshed "
                   f"({refreshed[0]} .. {refreshed[-1]}).")
    else:
        click.echo("Backfill complete: no months to refresh.")


@click.command("enable-retention")
@click.option("--window", default="13 months", show_default=True,
              help="Drop request_logs chunks older than this interval.")
@click.option("--dry-run/--force", "dry_run", default=True,
              help="Dry run (the default) only reports; --force adds the retention policy.")
@with_appcontext
def enable_retention_cmd(window, dry_run):
    """Report — and only with --force, enable — the request_logs retention policy.

    Deliberately an operator command rather than a migration: ``entrypoint.sh`` runs
    ``flask db upgrade`` at container start, so a migration calling
    ``add_retention_policy`` would begin deleting data on the next deploy and make the
    "dry run first" gate unenforceable.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"enable-retention requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    with _autocommit_connection() as conn:
        existing = _retention_drop_after(conn)
        if existing is not None:
            click.echo(f"request_logs already has a retention policy (drop_after = {existing}); "
                       f"no change made. The requested --window '{window}' was NOT applied — "
                       f"this command never edits an existing policy. To change the window run "
                       f"SELECT remove_retention_policy('request_logs') and then re-run.")
            return

        try:
            conn.execute(text("SELECT CAST(:w AS interval)"), {"w": window}).scalar()
        except DBAPIError:
            click.echo(f"Error: --window '{window}' is not a valid PostgreSQL interval.")
            raise SystemExit(1)

        click.echo(f"Retention window: {window}")
        dropped, oldest, newest = conn.execute(text(
            "SELECT COUNT(*), MIN(time), MAX(time) FROM request_logs "
            "WHERE time < now() - CAST(:w AS interval)"
        ), {"w": window}).one()
        click.echo(f"request_logs rows that would eventually be dropped: {dropped}"
                   + (f" ({_fmt(oldest)} .. {_fmt(newest)})" if dropped else ""))

        aggregates = _continuous_aggregates(conn)
        for view in aggregates:
            earliest = conn.execute(text(
                f'SELECT MIN("{_bucket_column(conn, view)}") FROM "{view}"'
            )).scalar()
            start_offset = conn.execute(text(
                "SELECT config->>'start_offset' FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_refresh_continuous_aggregate' AND hypertable_name = :v"
            ), {"v": view}).scalar()
            click.echo(f"  {view}: earliest bucket {_fmt(earliest)}, "
                       f"refresh start_offset {start_offset or 'none'}")
            if earliest is not None and start_offset is not None:
                floor_, wider = conn.execute(text(
                    "SELECT now() - CAST(:o AS interval), "
                    "CAST(:o AS interval) > CAST(:w AS interval)"
                ), {"o": start_offset, "w": window}).one()
                click.echo("      earliest bucket is "
                           + ("outside" if earliest < floor_ else "inside")
                           + f" the refresh window (start_offset floor {_fmt(floor_)})")
                if wider:
                    # Contract (d): a scheduled refresh reaching past the retention
                    # boundary recomputes those buckets as empty and deletes them.
                    click.echo(f"      WARNING: start_offset ({start_offset}) is wider than the "
                               f"retention window ({window}); the refresh policy would reach into "
                               f"dropped chunks and silently erase materialised rows.")

        if ENTITY_AGGREGATE not in aggregates:
            click.echo(f"Error: '{ENTITY_AGGREGATE}' does not exist. Retention would destroy "
                       f"per-entity history that is in no aggregate. Run 'flask db upgrade' first.")
            raise SystemExit(1)
        # Retention deletes raw chunks, so everything it deletes has to be materialised
        # already. "Not empty" cannot answer that: the aggregate is created WITH NO DATA
        # but carries materialized_only = false and an hourly refresh policy with a
        # 30-day start_offset, so ordinary traffic makes it non-empty within an hour of
        # deploy — a COUNT(*) guard passes forever after while no historical backfill has
        # ever run. The question that decides whether history survives is the one
        # _entity_aggregate_covers asks of this same aggregate before the per-entity
        # charts are allowed to read it: is its earliest materialised bucket at or before
        # the oldest raw row that still exists? If it is, everything retention can drop is
        # held twice. If it is not, the gap between those two timestamps lives only in the
        # chunks retention is about to delete.
        earliest = conn.execute(text(
            f'SELECT MIN("{_bucket_column(conn, ENTITY_AGGREGATE)}") '
            f'FROM {_materialization_hypertable(conn, ENTITY_AGGREGATE)}'
        )).scalar()
        raw_oldest = conn.execute(text("SELECT MIN(time) FROM request_logs")).scalar()
        if earliest is None:
            click.echo(f"Error: '{ENTITY_AGGREGATE}' is empty — the backfill has not been run. "
                       f"Enabling retention now would destroy per-entity history that is in no "
                       f"aggregate. Run 'flask backfill-aggregate' first.")
            raise SystemExit(1)
        if raw_oldest is not None and earliest > raw_oldest:
            hint = raw_oldest.astimezone(timezone.utc)
            click.echo(
                f"Error: '{ENTITY_AGGREGATE}' does not cover the history retention would drop.\n"
                f"  earliest materialised bucket:      {_fmt(earliest)}\n"
                f"  oldest surviving request_logs row: {_fmt(raw_oldest)}\n"
                f"  Everything between those two timestamps is held only in raw chunks, and\n"
                f"  retention deletes them. Run 'flask backfill-aggregate --from {hint:%Y-%m}'\n"
                f"  first, then re-run this command."
            )
            raise SystemExit(1)
        click.echo(f"{ENTITY_AGGREGATE} covers request_logs back to {_fmt(earliest)} "
                   f"(oldest raw row {_fmt(raw_oldest)}).")

        if dry_run:
            click.echo("Dry run: no policy added. Re-run with --force to enable retention.")
            return

        job_id = conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => CAST(:w AS interval))"
        ), {"w": window}).scalar()
        click.echo(f"Retention enabled on request_logs (drop_after = {window}, job {job_id}).")
