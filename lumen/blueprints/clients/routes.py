from datetime import datetime, timezone
from http import HTTPStatus

from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, select

from lumen.decorators import admin_required, is_admin, login_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_manager import EntityManager
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.entity_stat import EntityStat
from lumen.services.crypto import hash_api_key
from lumen.services.llm import get_model_access_status, get_model_status, has_model_consent
from lumen.blueprints.profile.routes import _get_profile_data

clients_bp = Blueprint("clients", __name__)


def _get_user_clients(entity_id: int):
    assocs = db.session.execute(select(EntityManager).filter_by(user_entity_id=entity_id)).scalars().all()
    client_ids = [a.client_entity_id for a in assocs]
    if not client_ids:
        return []
    return db.session.execute(
        select(Entity)
        .where(
            Entity.id.in_(client_ids),
            Entity.entity_type == "client",
            Entity.active == True,
        )
        .order_by(Entity.name)
    ).scalars().all()



@clients_bp.route("/clients", methods=["GET"])
@login_required
def index():
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)

    if is_admin(entity):
        clients = db.session.execute(
            select(Entity).filter_by(entity_type="client").order_by(Entity.name)
        ).scalars().all()
    else:
        clients = _get_user_clients(entity_id)

    client_ids = [c.id for c in clients]
    if client_ids:
        manager_counts = {
            row[0]: row[1]
            for row in db.session.execute(
                select(
                    EntityManager.client_entity_id,
                    func.count(EntityManager.id),
                )
                .where(EntityManager.client_entity_id.in_(client_ids))
                .group_by(EntityManager.client_entity_id)
            ).all()
        }
        client_stats = {
            row.entity_id: {
                "requests": int(row.requests or 0),
                "tokens": int((row.input_tokens or 0) + (row.output_tokens or 0)),
                "cost": float(row.cost or 0),
            }
            for row in db.session.execute(select(EntityStat).where(EntityStat.entity_id.in_(client_ids))).scalars().all()
        }
        total_requests = sum(s["requests"] for s in client_stats.values())
        total_tokens = sum(s["tokens"] for s in client_stats.values())
        total_cost = sum(s["cost"] for s in client_stats.values())
    else:
        manager_counts = {}
        client_stats = {}
        total_requests = 0
        total_tokens = 0
        total_cost = 0.0

    return render_template(
        "clients.html",
        clients=clients,
        manager_counts=manager_counts,
        client_stats=client_stats,
        total_requests=total_requests,
        total_tokens=total_tokens,
        total_cost=total_cost,
    )


@clients_bp.route("/clients/<int:sid>")
@login_required
def detail(sid):
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)
    client = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))

    if not is_admin(entity):
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, client_entity_id=sid)
        ).scalar_one_or_none()
        if not assoc:
            abort(HTTPStatus.FORBIDDEN)

    data = _get_profile_data(sid)

    managers = db.session.execute(
        select(Entity)
        .join(EntityManager, EntityManager.user_entity_id == Entity.id)
        .where(EntityManager.client_entity_id == sid)
        .order_by(Entity.name)
    ).scalars().all()

    all_models = db.session.execute(select(ModelConfig).order_by(ModelConfig.model_name)).scalars().all()
    usage_by_model = {u["model_name"]: u for u in data.get("model_usage", [])}
    model_access_list = []
    for mc in all_models:
        access_status = get_model_access_status(sid, mc.id)
        consented = has_model_consent(sid, mc.id) if access_status == "graylist" else None
        u = usage_by_model.get(mc.model_name, {})
        model_access_list.append({
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

    return render_template(
        "client_detail.html",
        client=client,
        managers=managers,
        model_access_list=model_access_list,
        **data,
    )


@clients_bp.route("/clients/<int:sid>/toggle", methods=["POST"])
@admin_required
def toggle_client(sid):
    client = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))
    client.active = not client.active
    db.session.commit()
    return jsonify({"active": client.active})


@clients_bp.route("/clients", methods=["POST"])
@admin_required
def create_client():
    data = request.get_json() or request.form
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Client name required"}), HTTPStatus.BAD_REQUEST

    client = Entity(
        entity_type="client",
        name=name,
        initials=name[:2].upper(),
        active=True,
    )
    db.session.add(client)
    db.session.commit()

    return jsonify({"id": client.id, "name": client.name}), HTTPStatus.CREATED


@clients_bp.route("/clients/<int:sid>", methods=["DELETE"])
@admin_required
def delete_client(sid):
    client = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))
    client.active = False
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@clients_bp.route("/clients/<int:sid>/users/search")
@admin_required
def search_client_users(sid):
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"users": []})

    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))

    existing_ids = {
        a.user_entity_id
        for a in db.session.execute(select(EntityManager).filter_by(client_entity_id=sid)).scalars().all()
    }

    stmt = (
        select(Entity)
        .where(
            Entity.entity_type == "user",
            Entity.active == True,
            db.or_(Entity.email.ilike(f"%{q}%"), Entity.name.ilike(f"%{q}%")),
        )
        .order_by(Entity.name)
        .limit(10)
    )
    if existing_ids:
        stmt = stmt.where(~Entity.id.in_(existing_ids))

    users = db.session.execute(stmt).scalars().all()
    return jsonify({"users": [{"id": u.id, "name": u.name, "email": u.email} for u in users]})


@clients_bp.route("/clients/<int:sid>/users", methods=["POST"])
@admin_required
def add_client_manager(sid):
    data = request.get_json() or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Email required"}), HTTPStatus.BAD_REQUEST

    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))

    user = db.session.execute(select(Entity).filter_by(email=email, entity_type="user")).scalar_one_or_none()
    if not user:
        return jsonify({"error": "User not found"}), HTTPStatus.NOT_FOUND

    existing = db.session.execute(
        select(EntityManager).filter_by(user_entity_id=user.id, client_entity_id=sid)
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "User already manages this client"}), HTTPStatus.CONFLICT

    new_assoc = EntityManager(user_entity_id=user.id, client_entity_id=sid)
    db.session.add(new_assoc)
    db.session.commit()

    return jsonify({"user_id": user.id, "name": user.name, "email": user.email}), HTTPStatus.CREATED


@clients_bp.route("/clients/<int:sid>/users/<int:uid>", methods=["DELETE"])
@admin_required
def remove_client_manager(sid, uid):
    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))

    target_assoc = db.session.execute(
        select(EntityManager).filter_by(user_entity_id=uid, client_entity_id=sid)
    ).scalar_one_or_none()
    if not target_assoc:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    db.session.delete(target_assoc)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@clients_bp.route("/clients/<int:sid>/keys", methods=["POST"])
@login_required
def create_client_key(sid):
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)
    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))

    if not is_admin(entity):
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, client_entity_id=sid)
        ).scalar_one_or_none()
        if not assoc:
            return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN

    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    key = (data.get("key") or "").strip()

    if not key or not key.startswith("sk_"):
        return jsonify({"error": "Invalid key"}), HTTPStatus.BAD_REQUEST

    key_hash = hash_api_key(key)
    if db.session.execute(select(APIKey).filter_by(key_hash=key_hash)).scalar_one_or_none():
        return jsonify({"error": "Key already exists"}), HTTPStatus.CONFLICT

    api_key = APIKey(
        entity_id=sid,
        name=name or "Unnamed Key",
        key_hash=key_hash,
        key_hint=f"{key[:7]}...{key[-4:]}",
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({"id": api_key.id, "name": api_key.name, "key": key}), HTTPStatus.CREATED


@clients_bp.route("/clients/<int:sid>/keys/<int:kid>", methods=["DELETE"])
@login_required
def delete_client_key(sid, kid):
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)

    if not is_admin(entity):
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, client_entity_id=sid)
        ).scalar_one_or_none()
        if not assoc:
            return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN

    api_key = db.get_or_404(APIKey, kid)
    if api_key.entity_id != sid:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    api_key.active = False
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@clients_bp.route("/clients/<int:sid>/consent/<path:model_name>", methods=["POST"])
@login_required
def client_consent(sid, model_name):
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)
    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="client"))

    if not is_admin(entity):
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, client_entity_id=sid)
        ).scalar_one_or_none()
        if not assoc:
            return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN

    config = db.first_or_404(select(ModelConfig).filter_by(model_name=model_name, active=True))

    if get_model_access_status(sid, config.id) != "graylist":
        return jsonify({"error": "Model is not graylisted for this client"}), HTTPStatus.BAD_REQUEST

    if not has_model_consent(sid, config.id):
        db.session.add(EntityModelConsent(
            entity_id=sid,
            model_config_id=config.id,
            consented_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()

    return jsonify({"ok": True}), HTTPStatus.OK
