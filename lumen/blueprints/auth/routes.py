import hashlib

from flask import Blueprint, redirect, url_for, session, render_template, current_app

from lumen.extensions import db, oauth
from lumen.models.entity import Entity
from lumen.models.entity_model_balance import EntityModelBalance
from lumen.models.model_config import ModelConfig
from lumen.models.entity_model_limit import EntityModelLimit
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

    # Per-user model limits from users.<email>.models
    models_cfg = users_cfg.get(email, {}).get("models", {})
    existing_limits = {lim.model_config_id: lim
                       for lim in EntityModelLimit.query.filter_by(entity_id=entity.id).all()}

    desired_model_ids = set()
    for model_key, limit_def in models_cfg.items():
        if model_key == "default":
            model_config_id = None
        else:
            mc = ModelConfig.query.filter_by(model_name=model_key).first()
            if mc is None:
                continue
            model_config_id = mc.id
        desired_model_ids.add(model_config_id)

        max_tokens = limit_def.get("max", 0)
        refresh_tokens = limit_def.get("refresh", 0)
        starting_tokens = limit_def.get("starting", max_tokens)

        if model_config_id in existing_limits:
            lim = existing_limits[model_config_id]
            lim.max_tokens = max_tokens
            lim.refresh_tokens = refresh_tokens
            lim.starting_tokens = starting_tokens
            lim.config_managed = True
        else:
            db.session.add(EntityModelLimit(
                entity_id=entity.id,
                model_config_id=model_config_id,
                max_tokens=max_tokens,
                refresh_tokens=refresh_tokens,
                starting_tokens=starting_tokens,
                config_managed=True,
            ))

    # Remove config_managed limits no longer in yaml for this user
    for model_config_id, lim in existing_limits.items():
        if lim.config_managed and model_config_id not in desired_model_ids:
            db.session.delete(lim)


@auth_bp.route("/")
def landing():
    if session.get("entity_id"):
        return redirect(url_for("chat.chat_page"))
    return render_template("landing.html")


@auth_bp.route("/login")
def login():
    redirect_uri = url_for("auth.callback", _external=True)
    params = current_app.config.get("OAUTH2_PARAMS", {})
    return oauth.provider.authorize_redirect(redirect_uri=redirect_uri, **params)


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
