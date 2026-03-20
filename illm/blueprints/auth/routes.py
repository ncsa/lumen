import hashlib

from flask import Blueprint, redirect, url_for, session, render_template, current_app

from illm.extensions import db, oauth
from illm.models.entity import Entity
from illm.models.entity_model_balance import EntityModelBalance
from illm.models.model_config import ModelConfig
from illm.models.entity_model_limit import EntityModelLimit

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


def seed_model_limits(entity: Entity):
    """Create EntityModelLimit rows for all active models using models.yaml defaults."""
    from flask import current_app

    yaml_data = current_app.config.get("YAML_DATA", {})
    tokens_cfg = yaml_data.get("users", {}).get("tokens", {})
    maximum = tokens_cfg.get("maximum", 0)
    refresh = tokens_cfg.get("refresh", 0)
    starting = tokens_cfg.get("starting", maximum)

    for model_config in ModelConfig.query.filter_by(active=True).all():
        existing = EntityModelLimit.query.filter_by(
            entity_id=entity.id, model_config_id=model_config.id
        ).first()
        if not existing:
            db.session.add(EntityModelLimit(
                entity_id=entity.id,
                model_config_id=model_config.id,
                max_tokens=maximum,
                refresh_tokens=refresh,
                starting_tokens=starting,
            ))
        if not EntityModelBalance.query.filter_by(entity_id=entity.id, model_config_id=model_config.id).first():
            db.session.add(EntityModelBalance(
                entity_id=entity.id,
                model_config_id=model_config.id,
                tokens_left=starting,
            ))


@auth_bp.route("/")
def landing():
    if session.get("entity_id"):
        return redirect(url_for("chat.chat_page"))
    return render_template("landing.html")


@auth_bp.route("/login")
def login():
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.provider.authorize_redirect(redirect_uri=redirect_uri)


@auth_bp.route("/callback")
def callback():
    token = oauth.provider.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.provider.userinfo()

    email = userinfo.get("email")
    if not email:
        return "OAuth2 provider did not return an email address.", 400

    name = userinfo.get("name") or userinfo.get("given_name") or email.split("@")[0]

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
        seed_model_limits(entity)
        db.session.commit()
    elif not entity.active:
        return "Account disabled.", 403
    else:
        entity.name = name
        entity.initials = make_initials(name)
        db.session.commit()

    yaml_data = current_app.config.get("YAML_DATA", {})
    admin_emails = yaml_data.get("users", {}).get("admins", [])

    session["entity_id"] = entity.id
    session["entity_name"] = entity.name
    session["initials"] = entity.initials
    session["gravatar_hash"] = entity.gravatar_hash or ""
    session["is_admin"] = email in admin_emails
    return redirect(url_for("chat.chat_page"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.landing"))
