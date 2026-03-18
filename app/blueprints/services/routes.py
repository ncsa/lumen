from flask import Blueprint, render_template, request, jsonify, session

from app.decorators import login_required, admin_required
from app.extensions import db
from app.models.entity import Entity
from app.models.entity_manager import EntityManager

services_bp = Blueprint("services", __name__)


def _get_user_services(entity_id: int):
    assocs = EntityManager.query.filter_by(user_entity_id=entity_id).all()
    service_ids = [a.service_entity_id for a in assocs]
    if not service_ids:
        return []
    return (
        Entity.query.filter(
            Entity.id.in_(service_ids),
            Entity.entity_type == "service",
            Entity.active == True,
        )
        .order_by(Entity.name)
        .all()
    )


@services_bp.route("/services", methods=["GET"])
@login_required
def index():
    services = _get_user_services(session["entity_id"])
    return render_template("services.html", services=services)


@services_bp.route("/services", methods=["POST"])
@admin_required
def create_service():
    data = request.get_json() or request.form
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Service name required"}), 400

    entity_id = session["entity_id"]

    service = Entity(
        entity_type="service",
        name=name,
        initials=name[:2].upper(),
        active=True,
    )
    db.session.add(service)
    db.session.flush()

    assoc = EntityManager(user_entity_id=entity_id, service_entity_id=service.id)
    db.session.add(assoc)
    db.session.commit()

    return jsonify({"id": service.id, "name": service.name}), 201


@services_bp.route("/services/<int:sid>", methods=["DELETE"])
@login_required
def delete_service(sid):
    entity_id = session["entity_id"]

    assoc = EntityManager.query.filter_by(
        user_entity_id=entity_id, service_entity_id=sid
    ).first()
    if not assoc:
        return jsonify({"error": "Forbidden"}), 403

    service = Entity.query.get_or_404(sid)
    service.active = False
    db.session.commit()
    return "", 204


@services_bp.route("/services/<int:sid>/users", methods=["POST"])
@login_required
def add_service_user(sid):
    entity_id = session["entity_id"]

    assoc = EntityManager.query.filter_by(
        user_entity_id=entity_id, service_entity_id=sid
    ).first()
    if not assoc:
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json() or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Email required"}), 400

    user = Entity.query.filter_by(email=email, entity_type="user").first()
    if not user:
        return jsonify({"error": "User not found"}), 404

    existing = EntityManager.query.filter_by(
        user_entity_id=user.id, service_entity_id=sid
    ).first()
    if existing:
        return jsonify({"error": "User already manages this service"}), 409

    new_assoc = EntityManager(user_entity_id=user.id, service_entity_id=sid)
    db.session.add(new_assoc)
    db.session.commit()

    return jsonify({"user_id": user.id, "name": user.name}), 201


@services_bp.route("/services/<int:sid>/users/<int:uid>", methods=["DELETE"])
@login_required
def remove_service_user(sid, uid):
    entity_id = session["entity_id"]

    assoc = EntityManager.query.filter_by(
        user_entity_id=entity_id, service_entity_id=sid
    ).first()
    if not assoc:
        return jsonify({"error": "Forbidden"}), 403

    target_assoc = EntityManager.query.filter_by(
        user_entity_id=uid, service_entity_id=sid
    ).first()
    if not target_assoc:
        return jsonify({"error": "Not found"}), 404

    db.session.delete(target_assoc)
    db.session.commit()
    return "", 204
