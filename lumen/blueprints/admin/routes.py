from flask import Blueprint, render_template, request, redirect, url_for, abort, jsonify
from sqlalchemy import func, case

from lumen.decorators import admin_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_model_balance import EntityModelBalance
from lumen.models.entity_model_limit import EntityModelLimit
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_model_limit import GroupModelLimit
from lumen.models.model_config import ModelConfig
from lumen.services.llm import get_effective_limit

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_BIGINT_MAX = 9223372036854775807
_VALID_PER_PAGE = {25, 50, 100, 200}


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@admin_bp.route("/groups")
@admin_required
def groups():
    return render_template("admin/groups.html")


@admin_bp.route("/groups", methods=["POST"])
@admin_required
def create_group():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip() or None
    if not name:
        return redirect(url_for("admin.groups"))
    group = Group(name=name, description=description)
    db.session.add(group)
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=group.id))


@admin_bp.route("/groups/<int:gid>")
@admin_required
def group_detail(gid):
    group = Group.query.get_or_404(gid)
    members = group.members.join(Entity, GroupMember.entity_id == Entity.id).add_entity(Entity).all()
    limits = group.limits.all()
    models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()
    return render_template("admin/group_detail.html", group=group, members=members, limits=limits, models=models)


@admin_bp.route("/groups/<int:gid>", methods=["POST"])
@admin_required
def update_group(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    group.name = request.form.get("name", group.name).strip()
    group.description = request.form.get("description", "").strip() or None
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/toggle", methods=["POST"])
@admin_required
def toggle_group(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    group.active = not group.active
    db.session.commit()
    return jsonify({"active": group.active})


@admin_bp.route("/groups/<int:gid>/members", methods=["POST"])
@admin_required
def add_member(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    email = request.form.get("email", "").strip()
    entity = Entity.query.filter_by(email=email, entity_type="user").first()
    if entity:
        existing = GroupMember.query.filter_by(group_id=gid, entity_id=entity.id).first()
        if not existing:
            db.session.add(GroupMember(group_id=gid, entity_id=entity.id))
            db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/members/<int:mid>/remove", methods=["POST"])
@admin_required
def remove_member(gid, mid):
    member = GroupMember.query.get_or_404(mid)
    if member.group_id != gid:
        abort(404)
    if member.config_managed:
        abort(403)
    entity_id = member.entity_id
    db.session.delete(member)
    db.session.commit()
    back = request.form.get("back")
    if back == "user_limits":
        return redirect(url_for("admin.user_limits", eid=entity_id))
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/limits")
@admin_required
def group_limits(gid):
    group = Group.query.get_or_404(gid)
    limits = group.limits.all()
    return render_template("admin/group_limits.html", group=group, limits=limits)


@admin_bp.route("/groups/<int:gid>/limits", methods=["POST"])
@admin_required
def upsert_group_limit(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    raw_model = request.form.get("model_config_id", "").strip()
    model_config_id = int(raw_model) if raw_model else None
    max_tokens = int(request.form.get("max_tokens", -1))
    refresh_tokens = int(request.form.get("refresh_tokens", 0))
    starting_tokens = int(request.form.get("starting_tokens", 0))

    existing = GroupModelLimit.query.filter_by(
        group_id=gid, model_config_id=model_config_id
    ).first()
    if existing:
        existing.max_tokens = max_tokens
        existing.refresh_tokens = refresh_tokens
        existing.starting_tokens = starting_tokens
    else:
        db.session.add(GroupModelLimit(
            group_id=gid,
            model_config_id=model_config_id,
            max_tokens=max_tokens,
            refresh_tokens=refresh_tokens,
            starting_tokens=starting_tokens,
        ))
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/limits/<int:lid>/delete", methods=["POST"])
@admin_required
def delete_group_limit(gid, lid):
    limit = GroupModelLimit.query.get_or_404(lid)
    if limit.group_id != gid:
        abort(404)
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    db.session.delete(limit)
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


# ---------------------------------------------------------------------------
# Groups API
# ---------------------------------------------------------------------------

@admin_bp.route("/api/groups")
@admin_required
def api_groups():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = request.args.get("per_page", 25, type=int)
    if per_page not in _VALID_PER_PAGE:
        per_page = 25
    sort = request.args.get("sort", "name")
    order = request.args.get("order", "asc")

    member_count_sq = (
        db.session.query(
            GroupMember.group_id,
            func.count(GroupMember.id).label("member_count"),
        )
        .group_by(GroupMember.group_id)
        .subquery()
    )

    q = (
        db.session.query(Group, func.coalesce(member_count_sq.c.member_count, 0).label("member_count"))
        .outerjoin(member_count_sq, Group.id == member_count_sq.c.group_id)
        .filter(Group.name != "default")
    )

    sort_col = {
        "name": Group.name,
        "description": Group.description,
        "active": Group.active,
        "members": member_count_sq.c.member_count,
    }.get(sort, Group.name)

    q = q.order_by(sort_col.desc() if order == "desc" else sort_col.asc())

    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "description": g.description or "",
                "members": count,
                "active": g.active,
                "config_managed": g.config_managed,
            }
            for g, count in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@admin_bp.route("/users")
@admin_required
def users():
    total_users = Entity.query.filter_by(entity_type="user").count()
    stats = (
        db.session.query(
            func.coalesce(func.sum(APIKey.requests), 0),
            func.coalesce(func.sum(APIKey.input_tokens + APIKey.output_tokens), 0),
            func.coalesce(func.sum(APIKey.cost), 0),
        )
        .join(Entity, APIKey.entity_id == Entity.id)
        .filter(Entity.entity_type == "user")
        .one()
    )
    total_requests, total_tokens, total_cost = stats
    return render_template(
        "admin/users.html",
        total_users=total_users,
        total_requests=int(total_requests),
        total_tokens=int(total_tokens),
        total_cost=float(total_cost),
    )


@admin_bp.route("/users/<int:eid>/toggle", methods=["POST"])
@admin_required
def toggle_user(eid):
    entity = Entity.query.get_or_404(eid)
    entity.active = not entity.active
    db.session.commit()
    return jsonify({"active": entity.active})


@admin_bp.route("/users/<int:eid>/limits")
@admin_required
def user_limits(eid):
    entity = Entity.query.get_or_404(eid)
    limits = EntityModelLimit.query.filter_by(entity_id=eid).all()
    models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()

    effective = {}
    balance_rows = {b.model_config_id: b.tokens_left for b in EntityModelBalance.query.filter_by(entity_id=eid).all()}
    tokens_left = {}
    for model in models:
        eff = get_effective_limit(eid, model.id)
        effective[model.id] = eff
        if eff is not None and eff[0] != -2:
            tokens_left[model.id] = balance_rows.get(model.id, eff[2])

    memberships = GroupMember.query.filter_by(entity_id=eid).all()
    group_details = []
    for m in memberships:
        group = Group.query.get(m.group_id)
        if group and group.active:
            glimits = GroupModelLimit.query.filter_by(group_id=group.id).order_by(
                GroupModelLimit.model_config_id.nullsfirst()
            ).all()
            group_details.append((m, group, glimits))

    return render_template(
        "admin/user_limits.html",
        entity=entity,
        limits=limits,
        models=models,
        effective=effective,
        tokens_left=tokens_left,
        group_details=group_details,
    )


@admin_bp.route("/users/<int:eid>/limits", methods=["POST"])
@admin_required
def upsert_user_limit(eid):
    Entity.query.get_or_404(eid)
    raw_model = request.form.get("model_config_id", "").strip()
    model_config_id = int(raw_model) if raw_model else None
    max_tokens = int(request.form.get("max_tokens", -1))
    refresh_tokens = int(request.form.get("refresh_tokens", 0))
    starting_tokens = int(request.form.get("starting_tokens", 0))

    existing = EntityModelLimit.query.filter_by(
        entity_id=eid, model_config_id=model_config_id
    ).first()
    if existing:
        if existing.config_managed:
            abort(403)
        existing.max_tokens = max_tokens
        existing.refresh_tokens = refresh_tokens
        existing.starting_tokens = starting_tokens
    else:
        db.session.add(EntityModelLimit(
            entity_id=eid,
            model_config_id=model_config_id,
            max_tokens=max_tokens,
            refresh_tokens=refresh_tokens,
            starting_tokens=starting_tokens,
        ))
        if model_config_id is not None:
            if not EntityModelBalance.query.filter_by(entity_id=eid, model_config_id=model_config_id).first():
                db.session.add(EntityModelBalance(
                    entity_id=eid,
                    model_config_id=model_config_id,
                    tokens_left=starting_tokens,
                ))
    db.session.commit()
    return redirect(url_for("admin.user_limits", eid=eid))


@admin_bp.route("/users/<int:eid>/limits/<int:lid>/delete", methods=["POST"])
@admin_required
def delete_user_limit(eid, lid):
    limit = EntityModelLimit.query.get_or_404(lid)
    if limit.entity_id != eid:
        abort(404)
    if limit.config_managed:
        abort(403)
    db.session.delete(limit)
    db.session.commit()
    return redirect(url_for("admin.user_limits", eid=eid))


# ---------------------------------------------------------------------------
# Users API
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users")
@admin_required
def api_users():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = request.args.get("per_page", 25, type=int)
    if per_page not in _VALID_PER_PAGE:
        per_page = 25
    sort = request.args.get("sort", "name")
    order = request.args.get("order", "asc")
    search = request.args.get("search", "").strip()

    api_stats_sq = (
        db.session.query(
            APIKey.entity_id,
            func.coalesce(func.sum(APIKey.requests), 0).label("requests"),
            func.coalesce(func.sum(APIKey.input_tokens + APIKey.output_tokens), 0).label("tokens_used"),
            func.coalesce(func.sum(APIKey.cost), 0).label("cost"),
        )
        .group_by(APIKey.entity_id)
        .subquery()
    )

    balance_sq = (
        db.session.query(
            EntityModelBalance.entity_id,
            func.coalesce(func.sum(EntityModelBalance.tokens_left), 0).label("tokens_available"),
        )
        .group_by(EntityModelBalance.entity_id)
        .subquery()
    )

    unlimited_sq = (
        db.session.query(EntityModelLimit.entity_id)
        .filter(EntityModelLimit.max_tokens == -2)
        .distinct()
        .subquery()
    )

    tokens_avail_sort = case(
        (unlimited_sq.c.entity_id != None, _BIGINT_MAX),  # noqa: E711
        else_=func.coalesce(balance_sq.c.tokens_available, 0),
    )

    q = (
        db.session.query(
            Entity,
            func.coalesce(api_stats_sq.c.requests, 0).label("requests"),
            func.coalesce(api_stats_sq.c.tokens_used, 0).label("tokens_used"),
            func.coalesce(api_stats_sq.c.cost, 0).label("cost"),
            tokens_avail_sort.label("tokens_available"),
        )
        .filter(Entity.entity_type == "user")
        .outerjoin(api_stats_sq, Entity.id == api_stats_sq.c.entity_id)
        .outerjoin(balance_sq, Entity.id == balance_sq.c.entity_id)
        .outerjoin(unlimited_sq, Entity.id == unlimited_sq.c.entity_id)
    )

    if search:
        like = f"%{search}%"
        q = q.filter(Entity.name.ilike(like) | Entity.email.ilike(like))

    sort_col = {
        "name": Entity.name,
        "email": Entity.email,
        "active": Entity.active,
        "requests": func.coalesce(api_stats_sq.c.requests, 0),
        "tokens_used": func.coalesce(api_stats_sq.c.tokens_used, 0),
        "cost": func.coalesce(api_stats_sq.c.cost, 0),
        "tokens_available": tokens_avail_sort,
    }.get(sort, Entity.name)

    q = q.order_by(sort_col.desc() if order == "desc" else sort_col.asc())

    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()

    # Batch-fetch group memberships for returned users
    user_ids = [entity.id for entity, *_ in rows]
    memberships = GroupMember.query.filter(GroupMember.entity_id.in_(user_ids)).all() if user_ids else []
    group_ids = {m.group_id for m in memberships}
    groups_by_id = {g.id: g.name for g in Group.query.filter(Group.id.in_(group_ids)).all()} if group_ids else {}
    user_groups = {}
    for m in memberships:
        user_groups.setdefault(m.entity_id, []).append(groups_by_id.get(m.group_id, ""))

    return jsonify({
        "users": [
            {
                "id": entity.id,
                "name": entity.name,
                "email": entity.email or "",
                "active": entity.active,
                "groups": user_groups.get(entity.id, []),
                "requests": int(requests),
                "tokens_used": int(tokens_used),
                "cost": float(cost),
                "tokens_available": -2 if int(tokens_available) == _BIGINT_MAX else int(tokens_available),
            }
            for entity, requests, tokens_used, cost, tokens_available in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })
