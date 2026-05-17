import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from flask import Blueprint, redirect, render_template, request, jsonify, session, url_for
from sqlalchemy import func, select

from lumen.decorators import login_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.conversation import Conversation
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.services.crypto import hash_api_key
from lumen.services.llm import get_pool_limit, get_model_access, get_model_access_status, get_model_status, has_model_consent

profile_bp = Blueprint("profile", __name__)


def _gravatar_url(email: str, size: int = 80) -> str:
    h = hashlib.md5((email or "").strip().lower().encode()).hexdigest()
    return f"https://www.gravatar.com/avatar/{h}?s={size}&d=mp"


def _entity_groups(eid: int) -> list:
    return db.session.execute(
        select(Group).join(GroupMember, Group.id == GroupMember.group_id)
        .where(GroupMember.entity_id == eid, Group.name != "default")
        .order_by(Group.name)
    ).scalars().all()


def _build_model_access_list(entity_id: int, usage_by_model: dict) -> list:
    """Build model access list for an entity, merging access status with usage stats."""
    all_models = db.session.execute(select(ModelConfig).order_by(ModelConfig.model_name)).scalars().all()
    result = []
    for mc in all_models:
        access_status = get_model_access_status(entity_id, mc.id)
        consented = has_model_consent(entity_id, mc.id) if access_status == "graylist" else None
        u = usage_by_model.get(mc.model_name, {})
        result.append({
            "model_name": mc.model_name,
            "model_url": url_for("models_page.detail", model_name=mc.model_name),
            "notice": mc.notice,
            "access_status": access_status,
            "consented": consented,
            "model_status": get_model_status(mc),
            "requests": u.get("requests", 0),
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cost": u.get("cost", 0.0),
            "last_used_at": u.get("last_used_at"),
        })
    return result


def _fetch_chat_stats(eid: int):
    chat_agg = db.session.execute(
        select(
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        ).filter_by(entity_id=eid, source="chat")
    ).one()
    conversation_count = db.session.scalar(
        select(func.count(Conversation.id)).filter_by(entity_id=eid)
    ) or 0
    return chat_agg, conversation_count


def _build_model_usage(eid: int):
    all_active_models = db.session.execute(
        select(ModelConfig).filter_by(active=True).order_by(ModelConfig.model_name)
    ).scalars().all()
    accessible_model_ids = {mc.id for mc in all_active_models if get_model_access(eid, mc.id)}

    usage_rows = db.session.execute(
        select(
            ModelStat.model_config_id,
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        )
        .where(ModelStat.entity_id == eid)
        .group_by(ModelStat.model_config_id)
    ).all()
    usage_by_id = {r[0]: r for r in usage_rows}

    models_to_show_ids = {mc.id for mc in all_active_models if mc.id in accessible_model_ids} | set(usage_by_id)
    all_relevant_models = (
        db.session.execute(
            select(ModelConfig).where(ModelConfig.id.in_(models_to_show_ids)).order_by(ModelConfig.model_name)
        ).scalars().all()
    ) if models_to_show_ids else []

    model_usage = []
    for mc in all_relevant_models:
        u = usage_by_id.get(mc.id)
        has_access = mc.id in accessible_model_ids
        status = "disabled" if not has_access else get_model_status(mc)
        model_usage.append({
            "model_name": mc.model_name,
            "requests": int(u[1] or 0) if u else 0,
            "input_tokens": int(u[2] or 0) if u else 0,
            "output_tokens": int(u[3] or 0) if u else 0,
            "cost": float(u[4] or 0) if u else 0.0,
            "last_used_at": u[5] if u else None,
            "status": status,
            "disabled": status == "disabled",
        })

    total_tokens_used = sum(r[2] + r[3] for r in usage_rows)
    total_cost = sum(float(r[4] or 0) for r in usage_rows)
    return model_usage, total_tokens_used, total_cost


def _build_coin_pool(eid: int):
    pool = get_pool_limit(eid)
    if pool is None:
        return None
    max_coins, refresh_coins, starting = pool
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=eid)).scalar_one_or_none()
    coins_left = float(balance.coins_left) if balance else starting
    last_refill_at = balance.last_refill_at if balance else None
    return {
        "coin_limit": max_coins,
        "coins_left": coins_left,
        "coins_per_hour": refresh_coins,
        "next_refill": (last_refill_at + timedelta(hours=1)) if (refresh_coins > 0 and last_refill_at) else None,
    }


def _get_profile_data(eid: int) -> dict:
    chat_agg, conversation_count = _fetch_chat_stats(eid)
    api_keys = db.session.execute(select(APIKey).filter_by(entity_id=eid).order_by(APIKey.created_at)).scalars().all()
    model_usage, total_tokens_used, total_cost = _build_model_usage(eid)
    coin_pool = _build_coin_pool(eid)
    return {
        "chat_agg": chat_agg,
        "conversation_count": conversation_count,
        "api_keys": api_keys,
        "model_usage": model_usage,
        "coin_pool": coin_pool,
        "status": {"total_tokens_used": total_tokens_used, "total_cost": total_cost},
    }


@profile_bp.route("/profile")
@login_required
def index():
    entity_id = session["entity_id"]
    data = _get_profile_data(entity_id)

    usage_by_model = {u["model_name"]: u for u in data.get("model_usage", [])}
    model_access_list = _build_model_access_list(entity_id, usage_by_model)

    profile_entity = db.session.get(Entity, entity_id)
    return render_template(
        "profile.html", **data,
        model_access_list=model_access_list,
        profile_entity=profile_entity,
        gravatar_url=_gravatar_url(profile_entity.email if profile_entity else "", size=230),
        profile_groups=_entity_groups(entity_id),
    )


@profile_bp.route("/profile/client/<int:sid>")
@login_required
def client_profile_page(sid):
    return redirect(url_for("clients.detail", sid=sid), HTTPStatus.MOVED_PERMANENTLY)


@profile_bp.route("/profile/keys/generate")
@login_required
def generate_key():
    key = "sk_" + secrets.token_urlsafe(32)
    return jsonify({"key": key})


@profile_bp.route("/profile/keys", methods=["POST"])
@login_required
def create_key():
    entity_id = session["entity_id"]
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    key = (data.get("key") or "").strip()

    if not key or not key.startswith("sk_"):
        return jsonify({"error": "Invalid key"}), HTTPStatus.BAD_REQUEST

    key_hash = hash_api_key(key)
    if db.session.execute(select(APIKey).filter_by(key_hash=key_hash)).scalar_one_or_none():
        return jsonify({"error": "Key already exists"}), HTTPStatus.CONFLICT

    api_key = APIKey(
        entity_id=entity_id,
        name=name or "Unnamed Key",
        key_hash=key_hash,
        key_hint=f"{key[:7]}...{key[-4:]}",
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({"id": api_key.id, "name": api_key.name, "key": key}), HTTPStatus.CREATED


@profile_bp.route("/profile/keys/<int:kid>", methods=["DELETE"])
@login_required
def delete_key(kid):
    entity_id = session["entity_id"]
    api_key = db.get_or_404(APIKey, kid)

    if api_key.entity_id != entity_id:
        return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN

    api_key.active = False
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@profile_bp.route("/profile/consent/<path:model_name>", methods=["POST"])
@login_required
def user_consent(model_name):
    entity_id = session["entity_id"]
    config = db.first_or_404(select(ModelConfig).filter_by(model_name=model_name, active=True))

    if get_model_access_status(entity_id, config.id) != "graylist":
        return jsonify({"error": "Model is not graylisted for this user"}), HTTPStatus.BAD_REQUEST

    if not has_model_consent(entity_id, config.id):
        db.session.add(EntityModelConsent(
            entity_id=entity_id,
            model_config_id=config.id,
            consented_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()

    return jsonify({"ok": True}), HTTPStatus.OK
