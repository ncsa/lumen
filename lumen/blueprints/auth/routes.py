import hashlib
from http import HTTPStatus

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, session, url_for
from flask_wtf.csrf import generate_csrf
from sqlalchemy import select

from lumen.extensions import db, oauth
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.services.llm import get_pool_limit
from lumen.timeutils import utcnow

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


def _groups_from_userinfo_rules(userinfo: dict, yaml_data: dict, existing: list[str]) -> list[str]:
    """Return additional group names matched by CILogon attribute rules, excluding already-desired ones."""

    def _rule_matches(rule):
        # A malformed rule (no field, or neither matcher) must fail closed:
        # matching it would silently add every user to the group.
        if not isinstance(rule, dict) or not rule.get("field"):
            return False
        value = userinfo.get(rule["field"]) or ""
        if "contains" in rule:
            return (rule.get("contains") or "") in value
        if "equals" in rule:
            return value == rule["equals"]
        return False

    added: list[str] = []
    for group_name, rules in (yaml_data.get("group_rules") or {}).items():
        if group_name in existing or group_name in added:
            continue
        rules = rules or []
        if rules and all(_rule_matches(rule) for rule in rules):
            added.append(group_name)
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


def sync_user_from_yaml(entity: Entity, email: str, yaml_data: dict, userinfo=None, extra_groups=None):
    """Sync config-managed group memberships. Does not commit.

    Desired memberships are: the 'default' group (when it exists in the DB),
    any extra_groups (dev login), and groups whose group_rules match userinfo.
    Explicit per-user assignment happens in the database, not config.yaml.
    """
    session.pop("_nav", None)
    desired_names = ["default"]
    for name in extra_groups or []:
        if name not in desired_names:
            desired_names.append(name)
    if userinfo:
        desired_names += _groups_from_userinfo_rules(userinfo, yaml_data, desired_names)

    desired_ids = set()
    for name in desired_names:
        group = db.session.execute(select(Group).filter_by(name=name)).scalar_one_or_none()
        if group:
            desired_ids.add(group.id)

    _reconcile_group_memberships(entity, desired_ids)

    # Initialize coin balance on first login so usage page shows starting coins immediately
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity.id)).scalar_one_or_none()
    if balance is None:
        pool = get_pool_limit(entity.id)
        if pool is not None and pool.max_coins != -2:
            starting_coins = pool.starting_coins
            db.session.add(EntityBalance(
                entity_id=entity.id,
                coins_left=starting_coins,
                last_refill_at=utcnow(),
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
    # Dev login bypasses OAuth, so restrict it to debug mode (local development).
    # We gate on the server-controlled debug flag rather than request.remote_addr,
    # which a co-located reverse proxy can mask as localhost.
    if not current_app.debug:
        abort(HTTPStatus.NOT_FOUND)
    yaml_data = current_app.config.get("YAML_DATA", {})
    dev_groups = current_app.config.get("DEV_USER_GROUPS", [])
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

    sync_user_from_yaml(entity, email, yaml_data, extra_groups=dev_groups)
    db.session.commit()

    session["entity_id"] = entity.id
    session["entity_name"] = entity.name
    session["initials"] = entity.initials
    session["gravatar_hash"] = entity.gravatar_hash or ""
    session["entity_email"] = email
    return redirect(url_for("chat.chat_page"))


@auth_bp.route("/callback")
def callback():
    token = oauth.provider.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.provider.userinfo()

    email = userinfo.get("email")
    if not email:
        return "OAuth2 provider did not return an email address.", HTTPStatus.BAD_REQUEST

    # Reject an email the provider explicitly marks unverified (account-takeover guard).
    # A missing email_verified claim is allowed; oauth2.allow_unverified_email accepts even false.
    ev = userinfo.get("email_verified")
    email_unverified = ev is False or (isinstance(ev, str) and ev.strip().lower() == "false")
    if email_unverified and not current_app.config.get("OAUTH2_ALLOW_UNVERIFIED_EMAIL", False):
        return "Your identity provider reports this email address as unverified.", HTTPStatus.FORBIDDEN

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
    session["entity_email"] = email
    return redirect(url_for("chat.chat_page"))


@auth_bp.route("/csrf-token")
def csrf_token():
    # Issue a freshly-timestamped CSRF token so long-lived pages can refresh
    # before the WTF_CSRF_TIME_LIMIT (1h) expires.
    return jsonify({"token": generate_csrf()})


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.landing"))
