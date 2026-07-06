import os
from http import HTTPStatus

import yaml
from flask import Blueprint, abort, current_app, jsonify, render_template, request, session, url_for
from sqlalchemy import func, select

from lumen.commands import sync_projects_from_yaml, write_config_yaml
from lumen.decorators import admin_required, is_admin, login_required
from lumen.extensions import db
from lumen.timeutils import utcnow
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_manager import EntityManager, get_managed_projects
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.entity_stat import EntityStat
from lumen.services.crypto import hash_api_key
from lumen.services.llm import get_model_access_status, has_model_consent
from lumen.blueprints.profile.routes import _get_profile_data

projects_bp = Blueprint("projects", __name__)

# Must match the options rendered by the frontend per-page selector.
_VALID_PER_PAGE = {25, 50, 100, 200}


def _require_project_access(entity_id: int, sid: int):
    """Abort 403 if entity_id is not an admin and does not manage project sid."""
    entity = db.session.get(Entity, entity_id)
    if not is_admin(entity):
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, project_entity_id=sid)
        ).scalar_one_or_none()
        if not assoc:
            abort(HTTPStatus.FORBIDDEN)


def _scoped_project_ids(entity_id, entity):
    """Full set of project ids visible to this caller (admins: all; others: managed)."""
    if is_admin(entity):
        return db.session.execute(
            select(Entity.id).where(Entity.entity_type == "project")
        ).scalars().all()
    return [c.id for c in get_managed_projects(entity_id)]


@projects_bp.route("/projects", methods=["GET"])
@login_required
def index():
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)

    # Summary cards reflect the full visible set, independent of the paginated table.
    project_ids = _scoped_project_ids(entity_id, entity)
    if project_ids:
        agg = db.session.execute(
            select(
                func.coalesce(func.sum(EntityStat.requests), 0),
                func.coalesce(func.sum(EntityStat.input_tokens + EntityStat.output_tokens), 0),
                func.coalesce(func.sum(EntityStat.cost), 0),
            ).where(EntityStat.entity_id.in_(project_ids))
        ).one()
        total_requests, total_tokens, total_cost = int(agg[0]), int(agg[1]), float(agg[2])
    else:
        total_requests = total_tokens = 0
        total_cost = 0.0

    return render_template(
        "projects.html",
        total_projects=len(project_ids),
        total_requests=total_requests,
        total_tokens=total_tokens,
        total_cost=total_cost,
    )


@projects_bp.route("/projects/data", methods=["GET"])
@login_required
def data():
    """Paginated project rows for the projects table (mirrors the admin users API)."""
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)
    admin = is_admin(entity)

    page = max(1, request.args.get("page", 1, type=int))
    per_page = request.args.get("per_page", 25, type=int)
    if per_page not in _VALID_PER_PAGE:
        per_page = 25
    sort = request.args.get("sort", "name")
    order = request.args.get("order", "asc")
    search = (request.args.get("search") or "").strip()
    show_disabled = request.args.get("show_disabled") in ("1", "true")

    mgr_sq = (
        select(
            EntityManager.project_entity_id.label("project_id"),
            func.count(EntityManager.id).label("mgr_count"),
        )
        .group_by(EntityManager.project_entity_id)
        .subquery()
    )

    stmt = (
        select(
            Entity,
            func.coalesce(EntityStat.requests, 0).label("requests"),
            func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0).label("tokens"),
            func.coalesce(EntityStat.cost, 0).label("cost"),
            func.coalesce(mgr_sq.c.mgr_count, 0).label("managers"),
        )
        .where(Entity.entity_type == "project")
        .outerjoin(EntityStat, Entity.id == EntityStat.entity_id)
        .outerjoin(mgr_sq, Entity.id == mgr_sq.c.project_id)
    )

    if admin:
        # Admins see all projects; disabled ones only when explicitly requested.
        if not show_disabled:
            stmt = stmt.where(Entity.active == True)
    else:
        # Non-admins only ever see the active projects they manage.
        managed_ids = [c.id for c in get_managed_projects(entity_id)]
        if not managed_ids:
            return jsonify({"projects": [], "total": 0, "page": page, "per_page": per_page})
        stmt = stmt.where(Entity.id.in_(managed_ids))

    if search:
        stmt = stmt.where(Entity.name.ilike(f"%{search}%"))

    sort_col = {
        "name": Entity.name,
        "managers": func.coalesce(mgr_sq.c.mgr_count, 0),
        "active": Entity.active,
        "requests": func.coalesce(EntityStat.requests, 0),
        "tokens": func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0),
        "cost": func.coalesce(EntityStat.cost, 0),
        "created": Entity.created_at,
    }.get(sort, Entity.name)
    direction = sort_col.desc().nullslast() if order == "desc" else sort_col.asc().nullslast()
    stmt = stmt.order_by(direction)

    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.session.execute(stmt.offset((page - 1) * per_page).limit(per_page)).all()

    return jsonify({
        "projects": [
            {
                "id": c.id,
                "name": c.name,
                "managers": int(managers),
                "active": c.active,
                "requests": int(requests),
                "tokens": int(tokens),
                "cost": float(cost),
                "created": c.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.created_at else None,
                "detail_url": url_for("projects.detail", sid=c.id),
            }
            for c, requests, tokens, cost, managers in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@projects_bp.route("/projects/<int:sid>")
@login_required
def detail(sid):
    entity_id = session["entity_id"]
    project = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    _require_project_access(entity_id, sid)

    data = _get_profile_data(sid)

    managers = db.session.execute(
        select(Entity)
        .join(EntityManager, EntityManager.user_entity_id == Entity.id)
        .where(EntityManager.project_entity_id == sid)
        .order_by(Entity.name)
    ).scalars().all()

    return render_template(
        "project_detail.html",
        project=project,
        managers=managers,
        **data,
    )


@projects_bp.route("/projects/<int:sid>/toggle", methods=["POST"])
@admin_required
def toggle_project(sid):
    project = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    project.active = not project.active
    db.session.commit()
    return jsonify({"active": project.active})


@projects_bp.route("/projects", methods=["POST"])
@admin_required
def create_project():
    data = request.get_json() or request.form
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Project name required"}), HTTPStatus.BAD_REQUEST

    project = Entity(
        entity_type="project",
        name=name,
        initials=name[:2].upper(),
        active=True,
    )
    db.session.add(project)
    db.session.commit()

    # Record the project in config.yaml with an empty entry so the file always reflects
    # which projects exist (they otherwise live only in the DB). Skip when the editor is
    # disabled or the file is not writable — the project still exists in the DB.
    config_path = current_app.config["CONFIG_YAML"]
    if current_app.config.get("CONFIG_EDITOR", True) and os.access(config_path, os.W_OK):
        try:
            with open(config_path) as f:
                cfg_data = yaml.safe_load(f) or {}
            projects_cfg = cfg_data.setdefault("projects", {})
            if name not in projects_cfg:
                projects_cfg[name] = {}
                write_config_yaml(config_path, cfg_data)
                current_app.config["YAML_DATA"] = cfg_data
        except OSError as e:
            current_app.logger.warning("create_project: could not write config.yaml: %s", e)

    # Apply the configured coin pool and model access defaults (projects.default or a
    # named override) immediately, so a new project starts with the right defaults
    # instead of waiting for the next config reload.
    sync_projects_from_yaml(current_app.config["YAML_DATA"])

    return jsonify({"id": project.id, "name": project.name}), HTTPStatus.CREATED


@projects_bp.route("/projects/<int:sid>", methods=["DELETE"])
@admin_required
def delete_project(sid):
    project = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    project.active = False
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@projects_bp.route("/projects/<int:sid>/users/search")
@admin_required
def search_project_users(sid):
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"users": []})

    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))

    existing_ids = {
        a.user_entity_id
        for a in db.session.execute(select(EntityManager).filter_by(project_entity_id=sid)).scalars().all()
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


@projects_bp.route("/projects/<int:sid>/users", methods=["POST"])
@admin_required
def add_project_manager(sid):
    data = request.get_json() or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Email required"}), HTTPStatus.BAD_REQUEST

    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))

    user = db.session.execute(select(Entity).filter_by(email=email, entity_type="user")).scalar_one_or_none()
    if not user:
        return jsonify({"error": "User not found"}), HTTPStatus.NOT_FOUND

    existing = db.session.execute(
        select(EntityManager).filter_by(user_entity_id=user.id, project_entity_id=sid)
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "User already manages this project"}), HTTPStatus.CONFLICT

    new_assoc = EntityManager(user_entity_id=user.id, project_entity_id=sid)
    db.session.add(new_assoc)
    db.session.commit()

    return jsonify({"user_id": user.id, "name": user.name, "email": user.email}), HTTPStatus.CREATED


@projects_bp.route("/projects/<int:sid>/users/<int:uid>", methods=["DELETE"])
@admin_required
def remove_project_manager(sid, uid):
    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))

    target_assoc = db.session.execute(
        select(EntityManager).filter_by(user_entity_id=uid, project_entity_id=sid)
    ).scalar_one_or_none()
    if not target_assoc:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    db.session.delete(target_assoc)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@projects_bp.route("/projects/<int:sid>/keys", methods=["POST"])
@login_required
def create_project_key(sid):
    entity_id = session["entity_id"]
    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    _require_project_access(entity_id, sid)

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


@projects_bp.route("/projects/<int:sid>/keys/<int:kid>", methods=["DELETE"])
@login_required
def delete_project_key(sid, kid):
    entity_id = session["entity_id"]
    _require_project_access(entity_id, sid)

    api_key = db.get_or_404(APIKey, kid)
    if api_key.entity_id != sid:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    db.session.delete(api_key)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@projects_bp.route("/projects/<int:sid>/consent/<path:model_name>", methods=["POST"])
@login_required
def project_consent(sid, model_name):
    entity_id = session["entity_id"]
    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    _require_project_access(entity_id, sid)

    config = db.first_or_404(select(ModelConfig).where(ModelConfig.model_name == model_name, ModelConfig.active))

    if get_model_access_status(sid, config.id) != "needs_ack":
        return jsonify({"error": "Model does not require acknowledgement for this project"}), HTTPStatus.BAD_REQUEST

    if not has_model_consent(sid, config.id):
        db.session.add(EntityModelConsent(
            entity_id=sid,
            model_config_id=config.id,
            consented_at=utcnow(),
        ))
        db.session.commit()

    return jsonify({"ok": True}), HTTPStatus.OK
