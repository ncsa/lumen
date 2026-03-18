import secrets
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify, session
from sqlalchemy import func

from app.decorators import login_required
from app.extensions import db
from app.models.api_key import APIKey
from app.models.entity import Entity
from app.models.entity_manager import EntityManager
from app.models.model_config import ModelConfig
from app.models.model_limit import ModelLimit
from app.models.model_stat import ModelStat

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

    model_usage = (
        db.session.query(
            ModelConfig.model_name,
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        )
        .join(ModelConfig, ModelStat.model_config_id == ModelConfig.id)
        .filter(ModelStat.entity_id == eid)
        .group_by(ModelConfig.model_name)
        .all()
    )

    # Build status summary from limits and stats
    limits = ModelLimit.query.filter_by(entity_id=eid).all()
    is_unlimited = any(l.token_limit == -2 for l in limits)
    active_limits = [l for l in limits if l.token_limit > 0]

    tokens_left = sum(l.tokens_left for l in active_limits)
    token_budget = sum(l.token_limit for l in active_limits)
    tokens_per_hour = sum(l.tokens_per_hour for l in limits if l.tokens_per_hour > 0)

    refillable = [l for l in limits if l.tokens_per_hour > 0 and l.last_refill_at]
    if refillable:
        next_refill = min(l.last_refill_at + timedelta(hours=1) for l in refillable)
    else:
        next_refill = None

    total_tokens_used = sum(int(row[2] or 0) + int(row[3] or 0) for row in model_usage)
    total_cost = sum(float(row[4] or 0) for row in model_usage)

    status = {
        "tokens_left": tokens_left,
        "token_budget": token_budget,
        "tokens_per_hour": tokens_per_hour,
        "next_refill": next_refill,
        "total_tokens_used": total_tokens_used,
        "total_cost": total_cost,
        "is_unlimited": is_unlimited,
        "has_limits": bool(active_limits) or is_unlimited,
    }

    return {
        "chat_agg": chat_agg,
        "api_keys": api_keys,
        "model_usage": model_usage,
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
    entity_id = session["entity_id"]

    assoc = EntityManager.query.filter_by(
        user_entity_id=entity_id, service_entity_id=sid
    ).first()
    if not assoc:
        return "Forbidden", 403

    service = Entity.query.get_or_404(sid)
    data = _get_usage_data(sid)
    return render_template("usage.html", **data, scope_entity=service)


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

    if APIKey.query.filter_by(key=key).first():
        return jsonify({"error": "Key already exists"}), 409

    api_key = APIKey(
        entity_id=entity_id,
        name=name or "Unnamed Key",
        key=key,
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({"id": api_key.id, "name": api_key.name, "key": api_key.key}), 201


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
