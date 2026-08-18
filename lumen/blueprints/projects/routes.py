import hashlib
from http import HTTPStatus

from flask import Blueprint, abort, jsonify, render_template, request, session, url_for
from sqlalchemy import case, func, select

from lumen.blueprints.admin.routes import apply_coin_pool_edit
from lumen.blueprints.profile.routes import _get_profile_data
from lumen.decorators import admin_required, is_admin, login_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_manager import (
    EntityManager,
    get_managed_projects,
    get_project_owner,
    is_project_owner,
)
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.services.crypto import hash_api_key
from lumen.services.llm import get_model_access_status
from lumen.timeutils import utcnow

projects_bp = Blueprint("projects", __name__)

# Must match the options rendered by the frontend per-page selector.
_VALID_PER_PAGE = {25, 50, 100, 200}
# Sentinel for "unlimited" sort key (mirrors admin/routes.py): -2 is the
# canonical "unlimited" value, encoded as BIGINT_MAX so it sorts last.
_BIGINT_MAX = 9223372036854775807


def _require_project_access(entity_id: int, sid: int):
    """Abort 403 if entity_id is not an admin and does not manage project sid."""
    entity = db.session.get(Entity, entity_id)
    if not is_admin(entity):
        assoc = db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, project_entity_id=sid)
        ).scalar_one_or_none()
        if not assoc:
            abort(HTTPStatus.FORBIDDEN)


def _require_project_admin(entity_id: int, sid: int):
    """Abort 403 unless caller is a global admin or the project's owner.

    Owner-level actions: add/remove managers, transfer ownership, toggle.
    Regular managers (non-owners) are rejected here.
    """
    entity = db.session.get(Entity, entity_id)
    if not is_admin(entity) and not is_project_owner(entity_id, sid):
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
    sort = request.args.get("sort", "last_used")
    order = request.args.get("order", "desc")
    search = (request.args.get("search") or "").strip()

    mgr_sq = (
        select(
            EntityManager.project_entity_id.label("project_id"),
            func.count(EntityManager.id).label("mgr_count"),
        )
        .group_by(EntityManager.project_entity_id)
        .subquery()
    )

    balance_sq = (
        select(
            EntityBalance.entity_id,
            EntityBalance.coins_left.label("coins_available"),
        )
        .subquery()
    )

    unlimited_sq = (
        select(EntityLimit.entity_id)
        .where(EntityLimit.max_coins == -2)
        .distinct()
        .subquery()
    )

    coins_avail_sort = case(
        (unlimited_sq.c.entity_id != None, _BIGINT_MAX),  # noqa: E711
        else_=func.coalesce(balance_sq.c.coins_available, 0),
    )

    stmt = (
        select(
            Entity,
            func.coalesce(EntityStat.requests, 0).label("requests"),
            func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0).label("tokens"),
            func.coalesce(EntityStat.cost, 0).label("cost"),
            func.coalesce(mgr_sq.c.mgr_count, 0).label("managers"),
            coins_avail_sort.label("coins_available"),
            EntityStat.last_used_at.label("last_used_at"),
        )
        .where(Entity.entity_type == "project")
        .outerjoin(EntityStat, Entity.id == EntityStat.entity_id)
        .outerjoin(mgr_sq, Entity.id == mgr_sq.c.project_id)
        .outerjoin(balance_sq, Entity.id == balance_sq.c.entity_id)
        .outerjoin(unlimited_sq, Entity.id == unlimited_sq.c.entity_id)
    )

    # Everyone sees disabled projects: admins see all, managers see theirs —
    # a manager must be able to reach a deactivated project to re-enable it.
    if not admin:
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
        "last_used": EntityStat.last_used_at,
        "requests": func.coalesce(EntityStat.requests, 0),
        "tokens": func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0),
        "coins_available": coins_avail_sort,
        "cost": func.coalesce(EntityStat.cost, 0),
        "created": Entity.created_at,
    }.get(sort, Entity.name)
    direction = sort_col.desc().nullslast() if order == "desc" else sort_col.asc().nullslast()
    stmt = stmt.order_by(direction)

    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.session.execute(stmt.offset((page - 1) * per_page).limit(per_page)).all()

    # Per-row data for the inline edit dialog: whether the caller owns the
    # project, and the project's own coin limit (None = inherited pool).
    owner_ids = {
        em.project_entity_id
        for em in db.session.execute(
            select(EntityManager).filter_by(user_entity_id=entity_id, is_owner=True)
        ).scalars().all()
    }
    page_ids = [c.id for c, *_ in rows]
    limits = {
        lim.entity_id: lim
        for lim in db.session.execute(
            select(EntityLimit).where(EntityLimit.entity_id.in_(page_ids))
        ).scalars().all()
    } if page_ids else {}

    return jsonify({
        "projects": [
            {
                "id": c.id,
                "name": c.name,
                "managers": int(managers),
                "active": c.active,
                "last_used": last_used_at.strftime("%Y-%m-%dT%H:%M:%SZ") if last_used_at else None,
                "requests": int(requests),
                "tokens": int(tokens),
                "coins_available": -2 if float(coins_available) >= _BIGINT_MAX else float(coins_available),
                "cost": float(cost),
                "created": c.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.created_at else None,
                "detail_url": url_for("projects.detail", sid=c.id),
                "is_owner": c.id in owner_ids,
                "max_coins": float(limits[c.id].max_coins) if c.id in limits else None,
                "refresh_coins": float(limits[c.id].refresh_coins) if c.id in limits else None,
            }
            for c, requests, tokens, cost, managers, coins_available, last_used_at in rows
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

    owner = get_project_owner(sid)
    owner_id = owner.id if owner else None
    entity = db.session.get(Entity, entity_id)
    can_manage = is_admin(entity) or (owner_id == entity_id)

    h = hashlib.md5(project.name.strip().lower().encode()).hexdigest()
    gravatar_url = f"https://www.gravatar.com/avatar/{h}?s=230&d=identicon&f=y"

    # The project's own limit row (not an inherited group/default pool), used to
    # prefill the edit dialog without materializing inherited values.
    project_limit = db.session.execute(
        select(EntityLimit).filter_by(entity_id=sid)
    ).scalar_one_or_none()

    return render_template(
        "project_detail.html",
        project=project,
        managers=managers,
        owner_id=owner_id,
        can_manage=can_manage,
        gravatar_url=gravatar_url,
        project_limit=project_limit,
        **data,
    )


@projects_bp.route("/projects/<int:sid>", methods=["PATCH"])
@login_required
def update_project(sid):
    """Edit a project's name/active (owner or admin) and coin pool (admin only)."""
    entity_id = session["entity_id"]
    project = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    _require_project_admin(entity_id, sid)

    data = request.get_json() or {}
    caller = db.session.get(Entity, entity_id)
    if ("max_coins" in data or "refresh_coins" in data) and not is_admin(caller):
        return jsonify({"error": "Only administrators can change coin limits"}), HTTPStatus.FORBIDDEN

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Project name required"}), HTTPStatus.BAD_REQUEST
        duplicate = db.session.execute(
            select(Entity).where(
                Entity.entity_type == "project", Entity.name == name, Entity.id != sid
            )
        ).scalar_one_or_none()
        if duplicate:
            return jsonify({"error": "A project with this name already exists"}), HTTPStatus.CONFLICT
        project.name = name
        project.initials = name[:2].upper()

    error = apply_coin_pool_edit(sid, data)
    if error:
        return jsonify({"error": error}), HTTPStatus.BAD_REQUEST

    if "active" in data:
        project.active = bool(data["active"])

    db.session.commit()
    return jsonify({"ok": True, "name": project.name, "active": project.active})


@projects_bp.route("/projects/<int:sid>/toggle", methods=["POST"])
@login_required
def toggle_project(sid):
    entity_id = session["entity_id"]
    project = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    _require_project_admin(entity_id, sid)
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

    owner_email = (data.get("owner_email") or "").strip()
    owner_user = None
    if owner_email:
        owner_user = db.session.execute(
            select(Entity).filter_by(email=owner_email, entity_type="user")
        ).scalar_one_or_none()
        if not owner_user:
            return jsonify({"error": "Owner user not found"}), HTTPStatus.NOT_FOUND

    project = Entity(
        entity_type="project",
        name=name,
        initials=name[:2].upper(),
        active=True,
    )
    db.session.add(project)
    db.session.flush()

    if owner_user:
        db.session.add(EntityManager(
            user_entity_id=owner_user.id,
            project_entity_id=project.id,
            is_owner=True,
        ))

    db.session.commit()

    return jsonify({"id": project.id, "name": project.name}), HTTPStatus.CREATED


@projects_bp.route("/projects/<int:sid>", methods=["DELETE"])
@admin_required
def delete_project(sid):
    project = db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    project.active = False
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@projects_bp.route("/projects/<int:sid>/users/search")
@login_required
def search_project_users(sid):
    entity_id = session["entity_id"]
    _require_project_admin(entity_id, sid)

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
            Entity.active == True,  # noqa: E712 — SQL comparison, not a truth check
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
@login_required
def add_project_manager(sid):
    entity_id = session["entity_id"]
    _require_project_admin(entity_id, sid)

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
@login_required
def remove_project_manager(sid, uid):
    entity_id = session["entity_id"]
    _require_project_admin(entity_id, sid)

    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))

    target_assoc = db.session.execute(
        select(EntityManager).filter_by(user_entity_id=uid, project_entity_id=sid)
    ).scalar_one_or_none()
    if not target_assoc:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    if target_assoc.is_owner:
        return jsonify({"error": "Transfer ownership before removing the owner"}), HTTPStatus.CONFLICT

    db.session.delete(target_assoc)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@projects_bp.route("/projects/<int:sid>/owner", methods=["POST"])
@login_required
def transfer_ownership(sid):
    entity_id = session["entity_id"]
    _require_project_admin(entity_id, sid)

    data = request.get_json() or {}
    new_owner_id = data.get("user_id")
    if not new_owner_id:
        return jsonify({"error": "user_id required"}), HTTPStatus.BAD_REQUEST

    db.first_or_404(select(Entity).filter_by(id=sid, entity_type="project"))
    new_owner = db.session.execute(
        select(Entity).filter_by(id=new_owner_id, entity_type="user")
    ).scalar_one_or_none()
    if not new_owner:
        return jsonify({"error": "User not found"}), HTTPStatus.NOT_FOUND

    old_owner_assoc = db.session.execute(
        select(EntityManager).filter_by(project_entity_id=sid, is_owner=True)
    ).scalar_one_or_none()

    new_assoc = db.session.execute(
        select(EntityManager).filter_by(user_entity_id=new_owner_id, project_entity_id=sid)
    ).scalar_one_or_none()
    if new_assoc is not None and new_assoc is old_owner_assoc:
        return jsonify({"error": "User is already the owner"}), HTTPStatus.CONFLICT

    if old_owner_assoc:
        old_owner_assoc.is_owner = False
    if new_assoc:
        new_assoc.is_owner = True
    else:
        db.session.add(EntityManager(
            user_entity_id=new_owner_id,
            project_entity_id=sid,
            is_owner=True,
        ))

    db.session.commit()
    return jsonify({"owner_id": new_owner_id}), HTTPStatus.OK


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

    row = db.session.execute(
        select(EntityModelConsent).filter_by(entity_id=sid, model_config_id=config.id)
    ).scalar_one_or_none()
    if row is None:
        row = EntityModelConsent(entity_id=sid, model_config_id=config.id)
        db.session.add(row)
    now = utcnow()
    if config.needs_ack and row.consented_at is None:
        row.consented_at = now
    if config.early_access and row.early_access_at is None:
        row.early_access_at = now
    db.session.commit()

    return jsonify({"ok": True}), HTTPStatus.OK
