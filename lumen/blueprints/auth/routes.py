import hashlib
from datetime import datetime, timezone
from http import HTTPStatus

from flask import Blueprint, abort, redirect, url_for, session, render_template, request, current_app
from sqlalchemy import select

from lumen.extensions import db, oauth
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.services.llm import get_pool_limit

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


def _desired_groups_from_config(email: str, yaml_data: dict) -> list[str]:
    """Return group names from users.default.groups and users.<email>.groups."""
    users_cfg = yaml_data.get("users", {})
    names: list[str] = ["default"]
    for name in users_cfg.get("default", {}).get("groups", []):
        if name not in names:
            names.append(name)
    for name in users_cfg.get(email, {}).get("groups", []):
        if name not in names:
            names.append(name)
    return names


def _groups_from_userinfo_rules(userinfo: dict, yaml_data: dict, existing: list[str]) -> list[str]:
    """Return additional group names matched by CILogon attribute rules, excluding already-desired ones."""
    added: list[str] = []
    for group_name, group_def in yaml_data.get("groups", {}).items():
        if group_name in existing or group_name in added:
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
                added.append(group_name)
                break
    return added


def _reconcile_group_memberships(entity: Entity, desired_ids: set) -> None:
    """Add missing and remove stale config_managed group memberships for entity."""
    existing_members = db.session.execute(select(GroupMember).filter_by(entity_id=entity.id)).scalars().all()
    existing_by_group = {m.group_id: m for m in existing_members}
    for group_id, member in existing_by_group.items():
        if member.config_managed and group_id not in desired_ids:
            db.session.delete(member)
    for group_id in desired_ids:
        if group_id not in existing_by_group:
            db.session.add(GroupMember(group_id=group_id, entity_id=entity.id, config_managed=True))


def _apply_user_model_overrides(entity: Entity, email: str, yaml_data: dict) -> None:
    """Reconcile per-user coin pool limits and model access whitelists from yaml. Does not commit."""
    user_cfg = yaml_data.get("users", {}).get(email, {})

    # Coin pool
    pool_cfg = user_cfg.get("pool") or (
        {"max": user_cfg["max"], "refresh": user_cfg.get("refresh", 0), "starting": user_cfg.get("starting", user_cfg["max"])}
        if "max" in user_cfg else None
    )
    if pool_cfg:
        max_coins = pool_cfg.get("max", 0)
        refresh_coins = pool_cfg.get("refresh", 0)
        starting_coins = pool_cfg.get("starting", max_coins)
        limit = db.session.execute(select(EntityLimit).filter_by(entity_id=entity.id)).scalar_one_or_none()
        if limit and limit.config_managed:
            limit.max_coins = max_coins
            limit.refresh_coins = refresh_coins
            limit.starting_coins = starting_coins
        elif not limit:
            db.session.add(EntityLimit(
                entity_id=entity.id,
                max_coins=max_coins,
                refresh_coins=refresh_coins,
                starting_coins=starting_coins,
                config_managed=True,
            ))
    else:
        limit = db.session.execute(select(EntityLimit).filter_by(entity_id=entity.id, config_managed=True)).scalar_one_or_none()
        if limit:
            db.session.delete(limit)

    # Model access whitelist
    allowed_models = user_cfg.get("models", [])
    existing_access = {
        a.model_config_id: a
        for a in db.session.execute(select(EntityModelAccess).filter_by(entity_id=entity.id)).scalars().all()
        if a.access_type == "whitelist"
    }
    desired_model_ids: set = set()
    for model_name in allowed_models:
        mc = db.session.execute(select(ModelConfig).filter_by(model_name=model_name)).scalar_one_or_none()
        if mc is None:
            continue
        desired_model_ids.add(mc.id)
        if mc.id not in existing_access:
            db.session.add(EntityModelAccess(entity_id=entity.id, model_config_id=mc.id, access_type="whitelist"))
    for model_config_id, acc in existing_access.items():
        if model_config_id not in desired_model_ids:
            db.session.delete(acc)


def sync_user_from_yaml(entity: Entity, email: str, yaml_data: dict, userinfo=None):
    """Sync group memberships and per-user model limits from yaml_data. Does not commit."""
    desired_names = _desired_groups_from_config(email, yaml_data)
    if userinfo:
        desired_names += _groups_from_userinfo_rules(userinfo, yaml_data, desired_names)

    desired_ids = set()
    for name in desired_names:
        group = db.session.execute(select(Group).filter_by(name=name)).scalar_one_or_none()
        if group:
            desired_ids.add(group.id)

    _reconcile_group_memberships(entity, desired_ids)
    _apply_user_model_overrides(entity, email, yaml_data)

    # Initialize coin balance on first login so usage page shows starting coins immediately
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity.id)).scalar_one_or_none()
    if balance is None:
        pool = get_pool_limit(entity.id)
        if pool is not None and pool[0] != -2:
            _, _, starting_coins = pool
            db.session.add(EntityBalance(
                entity_id=entity.id,
                coins_left=starting_coins,
                last_refill_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ))


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
        return "Dev login not configured.", HTTPStatus.FORBIDDEN
    if not current_app.debug and request.remote_addr not in ("127.0.0.1", "::1"):
        abort(HTTPStatus.NOT_FOUND)
    yaml_data = current_app.config.get("YAML_DATA", {})
    dev_groups = current_app.config.get("DEV_USER_GROUPS", [])
    if dev_groups:
        users = dict(yaml_data.get("users") or {})
        entry = dict(users.get(email) or {})
        existing = list(entry.get("groups") or [])
        for g in dev_groups:
            if g not in existing:
                existing.append(g)
        entry["groups"] = existing
        users[email] = entry
        yaml_data = {**yaml_data, "users": users}
    name = email.split("@")[0]

    entity = db.session.execute(select(Entity).filter_by(email=email, entity_type="user")).scalar_one_or_none()
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
        return "OAuth2 provider did not return an email address.", HTTPStatus.BAD_REQUEST

    name = userinfo.get("name") or userinfo.get("given_name") or email.split("@")[0]

    yaml_data = current_app.config.get("YAML_DATA", {})

    entity = db.session.execute(select(Entity).filter_by(email=email, entity_type="user")).scalar_one_or_none()
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
        return "Account disabled.", HTTPStatus.FORBIDDEN
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
