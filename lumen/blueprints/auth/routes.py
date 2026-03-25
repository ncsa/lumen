import hashlib

from flask import Blueprint, redirect, url_for, session, render_template, current_app

from lumen.extensions import db, oauth
from lumen.models.entity import Entity
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.group import Group
from lumen.models.group_member import GroupMember

auth_bp = Blueprint("auth", __name__)


def gravatar_md5(email: str) -> str:
    return hashlib.md5(email.strip().lower().encode()).hexdigest()


def make_initials(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    if len(parts) == 1 and parts[0]:
        return parts[0][:2].upper()
    return "??"


def sync_user_from_yaml(entity: Entity, email: str, yaml_data: dict, userinfo=None):
    """Sync group memberships and per-user model limits from yaml_data. Does not commit."""

    users_cfg = yaml_data.get("users", {})

    # Compute desired group names: implicit default + users.default.groups + users.<email>.groups
    desired_names = ["default"]
    for name in users_cfg.get("default", {}).get("groups", []):
        if name not in desired_names:
            desired_names.append(name)
    for name in users_cfg.get(email, {}).get("groups", []):
        if name not in desired_names:
            desired_names.append(name)

    # Auto-assign groups via CILogon attribute rules
    if userinfo:
        groups_cfg = yaml_data.get("groups", {})
        for group_name, group_def in groups_cfg.items():
            if group_name in desired_names:
                continue
            for rule in (group_def or {}).get("rules", []):
                field = rule.get("field")
                if not field:
                    continue
                field_value = userinfo.get(field) or ""
                if "contains" in rule:
                    matched = rule["contains"] in field_value
                elif "equals" in rule:
                    matched = field_value == rule["equals"]
                else:
                    matched = False
                if matched:
                    desired_names.append(group_name)
                    break

    # Resolve group names to IDs (skip unknown)
    desired_ids = set()
    for name in desired_names:
        group = Group.query.filter_by(name=name).first()
        if group:
            desired_ids.add(group.id)

    # Current memberships
    existing_members = GroupMember.query.filter_by(entity_id=entity.id).all()
    existing_by_group = {m.group_id: m for m in existing_members}

    # Remove config_managed memberships no longer desired
    for group_id, member in existing_by_group.items():
        if member.config_managed and group_id not in desired_ids:
            db.session.delete(member)

    # Add missing desired groups
    for group_id in desired_ids:
        if group_id not in existing_by_group:
            db.session.add(GroupMember(group_id=group_id, entity_id=entity.id, config_managed=True))

    # Per-user token pool from users.<email>.pool (or flat max/refresh/starting keys)
    user_cfg = users_cfg.get(email, {})
    pool_cfg = user_cfg.get("pool") or (
        {"max": user_cfg["max"], "refresh": user_cfg.get("refresh", 0), "starting": user_cfg.get("starting", user_cfg["max"])}
        if "max" in user_cfg else None
    )
    if pool_cfg:
        max_tokens = pool_cfg.get("max", 0)
        refresh_tokens = pool_cfg.get("refresh", 0)
        starting_tokens = pool_cfg.get("starting", max_tokens)
        limit = EntityLimit.query.filter_by(entity_id=entity.id).first()
        if limit and limit.config_managed:
            limit.max_tokens = max_tokens
            limit.refresh_tokens = refresh_tokens
            limit.starting_tokens = starting_tokens
        elif not limit:
            db.session.add(EntityLimit(
                entity_id=entity.id,
                max_tokens=max_tokens,
                refresh_tokens=refresh_tokens,
                starting_tokens=starting_tokens,
                config_managed=True,
            ))
    else:
        # Remove config_managed limit if no longer in yaml
        limit = EntityLimit.query.filter_by(entity_id=entity.id, config_managed=True).first()
        if limit:
            db.session.delete(limit)

    # Per-user model access from users.<email>.models list
    allowed_models = user_cfg.get("models", [])
    existing_access = {a.model_config_id: a
                       for a in EntityModelAccess.query.filter_by(entity_id=entity.id).all()
                       if a.allowed}  # only track config-managed allows

    desired_model_ids = set()
    for model_name in allowed_models:
        mc = ModelConfig.query.filter_by(model_name=model_name).first()
        if mc is None:
            continue
        desired_model_ids.add(mc.id)
        if mc.id not in existing_access:
            db.session.add(EntityModelAccess(
                entity_id=entity.id,
                model_config_id=mc.id,
                allowed=True,
            ))

    # Remove config-managed access rows no longer in yaml
    for model_config_id, acc in existing_access.items():
        if model_config_id not in desired_model_ids:
            db.session.delete(acc)


@auth_bp.route("/")
def landing():
    if session.get("entity_id"):
        return redirect(url_for("chat.chat_page"))
    has_provider = hasattr(oauth, "provider")
    has_dev = bool(current_app.config.get("DEV_USER"))
    return render_template("landing.html", has_provider=has_provider, has_dev=has_dev)


@auth_bp.route("/login")
def login():
    redirect_uri = url_for("auth.callback", _external=True)
    params = current_app.config.get("OAUTH2_PARAMS", {})
    return oauth.provider.authorize_redirect(redirect_uri=redirect_uri, **params)



@auth_bp.route("/devlogin")
def devlogin():
    email = current_app.config.get("DEV_USER")
    if not email:
        return "Dev login not configured.", 403
    yaml_data = current_app.config.get("YAML_DATA", {})
    name = email.split("@")[0]

    entity = Entity.query.filter_by(email=email, entity_type="user").first()
    if not entity:
        entity = Entity(
            entity_type="user",
            email=email,
            name=name,
            initials=make_initials(name),
            gravatar_hash=gravatar_md5(email),
            active=True,
        )
        db.session.add(entity)
        db.session.flush()

    sync_user_from_yaml(entity, email, yaml_data)
    db.session.commit()

    session["entity_id"] = entity.id
    session["entity_name"] = entity.name
    session["initials"] = entity.initials
    session["gravatar_hash"] = entity.gravatar_hash or ""
    return redirect(url_for("chat.chat_page"))


@auth_bp.route("/callback")
def callback():
    token = oauth.provider.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.provider.userinfo()

    email = userinfo.get("email")
    if not email:
        return "OAuth2 provider did not return an email address.", 400

    name = userinfo.get("name") or userinfo.get("given_name") or email.split("@")[0]

    yaml_data = current_app.config.get("YAML_DATA", {})

    entity = Entity.query.filter_by(email=email, entity_type="user").first()
    if not entity:
        entity = Entity(
            entity_type="user",
            email=email,
            name=name,
            initials=make_initials(name),
            gravatar_hash=gravatar_md5(email),
            active=True,
        )
        db.session.add(entity)
        db.session.flush()
    elif not entity.active:
        return "Account disabled.", 403
    else:
        entity.name = name
        entity.initials = make_initials(name)

    sync_user_from_yaml(entity, email, yaml_data, userinfo=userinfo)
    db.session.commit()

    session["entity_id"] = entity.id
    session["entity_name"] = entity.name
    session["initials"] = entity.initials
    session["gravatar_hash"] = entity.gravatar_hash or ""
    return redirect(url_for("chat.chat_page"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.landing"))
