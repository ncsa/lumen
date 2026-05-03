import secrets
from datetime import datetime, timedelta

from flask import Blueprint, redirect, render_template, request, jsonify, session, url_for
from sqlalchemy import func

from lumen.decorators import login_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.services.crypto import hash_api_key
from lumen.models.model_config import ModelConfig
from lumen.models.entity_balance import EntityBalance
from lumen.models.model_stat import ModelStat
from lumen.services.llm import get_pool_limit, get_model_access

usage_bp = Blueprint("usage", __name__)


def _get_usage_data(eid: int) -> dict:
    chat_agg = (
        db.session.query(
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        )
        .filter_by(entity_id=eid, source="chat")
        .one()
    )

    api_keys = APIKey.query.filter_by(entity_id=eid).order_by(APIKey.created_at).all()

    # Single token pool and accessible models
    pool = get_pool_limit(eid)
    balance = EntityBalance.query.filter_by(entity_id=eid).first()
    all_active_models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()
    accessible_model_ids = {mc.id for mc in all_active_models if get_model_access(eid, mc.id)}

    # Usage stats keyed by model_config_id
    usage_rows = (
        db.session.query(
            ModelStat.model_config_id,
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        )
        .filter(ModelStat.entity_id == eid)
        .group_by(ModelStat.model_config_id)
        .all()
    )
    usage_by_id = {r[0]: r for r in usage_rows}

    # Models to show: all accessible active models + inactive models with past usage
    models_to_show_ids = set()
    for mc in all_active_models:
        if mc.id in accessible_model_ids:
            models_to_show_ids.add(mc.id)
    for mid in usage_by_id:
        models_to_show_ids.add(mid)

    all_relevant_models = (
        ModelConfig.query
        .filter(ModelConfig.id.in_(models_to_show_ids))
        .order_by(ModelConfig.model_name)
        .all()
    ) if models_to_show_ids else []

    def model_status(mc):
        if not mc.active:
            return "disabled"
        endpoints = list(mc.endpoints)
        if not endpoints:
            return "down"
        healthy = sum(1 for e in endpoints if e.healthy)
        if healthy == 0:
            return "down"
        if healthy < len(endpoints):
            return "degraded"
        return "ok"

    model_usage = []
    for mc in all_relevant_models:
        u = usage_by_id.get(mc.id)
        has_access = mc.id in accessible_model_ids
        status = "disabled" if not has_access else model_status(mc)
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

    if pool is not None:
        max_coins, refresh_coins, starting = pool
        coins_left = float(balance.coins_left) if balance else starting
        last_refill_at = balance.last_refill_at if balance else None
        coin_pool = {
            "coin_limit": max_coins,
            "coins_left": coins_left,
            "coins_per_hour": refresh_coins,
            "next_refill": (last_refill_at + timedelta(hours=1)) if (refresh_coins > 0 and last_refill_at) else None,
        }
    else:
        coin_pool = None

    total_tokens_used = sum(r[2] + r[3] for r in usage_rows)
    total_cost = sum(float(r[4] or 0) for r in usage_rows)

    status = {
        "total_tokens_used": total_tokens_used,
        "total_cost": total_cost,
    }

    return {
        "chat_agg": chat_agg,
        "api_keys": api_keys,
        "model_usage": model_usage,
        "coin_pool": coin_pool,
        "status": status,
    }


@usage_bp.route("/usage")
@login_required
def index():
    data = _get_usage_data(session["entity_id"])
    return render_template("usage.html", **data, scope_entity=None)


@usage_bp.route("/usage/service/<int:sid>")
@login_required
def service_usage_page(sid):
    return redirect(url_for("clients.detail", sid=sid), 301)


@usage_bp.route("/usage/keys/generate")
@login_required
def generate_key():
    key = "sk_" + secrets.token_urlsafe(32)
    return jsonify({"key": key})


@usage_bp.route("/usage/keys", methods=["POST"])
@login_required
def create_key():
    entity_id = session["entity_id"]
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    key = (data.get("key") or "").strip()

    if not key or not key.startswith("sk_"):
        return jsonify({"error": "Invalid key"}), 400

    key_hash = hash_api_key(key)
    if APIKey.query.filter_by(key_hash=key_hash).first():
        return jsonify({"error": "Key already exists"}), 409

    api_key = APIKey(
        entity_id=entity_id,
        name=name or "Unnamed Key",
        key_hash=key_hash,
        key_hint=f"{key[:7]}...{key[-4:]}",
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({"id": api_key.id, "name": api_key.name, "key": key}), 201


@usage_bp.route("/usage/keys/<int:kid>", methods=["DELETE"])
@login_required
def delete_key(kid):
    entity_id = session["entity_id"]
    api_key = APIKey.query.get_or_404(kid)

    if api_key.entity_id != entity_id:
        return jsonify({"error": "Forbidden"}), 403

    api_key.active = False
    db.session.commit()
    return "", 204
