from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, abort, jsonify
from sqlalchemy import func, case, text

from lumen.decorators import admin_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_access import EntityModelAccess
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.group_limit import GroupLimit
from lumen.models.group_model_access import GroupModelAccess
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.services.llm import get_pool_limit

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
    group_limit = GroupLimit.query.filter_by(group_id=gid).first()
    group_model_access = GroupModelAccess.query.filter_by(group_id=gid).all()
    models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()
    return render_template(
        "admin/group_detail.html",
        group=group,
        members=members,
        group_limit=group_limit,
        group_model_access=group_model_access,
        models=models,
    )


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


@admin_bp.route("/groups/<int:gid>/pool", methods=["POST"])
@admin_required
def upsert_group_pool(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    max_tokens = int(request.form.get("max_tokens", 0))
    refresh_tokens = int(request.form.get("refresh_tokens", 0))
    starting_tokens = int(request.form.get("starting_tokens", 0))

    limit = GroupLimit.query.filter_by(group_id=gid).first()
    if limit:
        limit.max_tokens = max_tokens
        limit.refresh_tokens = refresh_tokens
        limit.starting_tokens = starting_tokens
    else:
        db.session.add(GroupLimit(
            group_id=gid,
            max_tokens=max_tokens,
            refresh_tokens=refresh_tokens,
            starting_tokens=starting_tokens,
        ))
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/pool/delete", methods=["POST"])
@admin_required
def delete_group_pool(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    GroupLimit.query.filter_by(group_id=gid).delete()
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/access", methods=["POST"])
@admin_required
def upsert_group_access(gid):
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    model_config_id = int(request.form.get("model_config_id"))
    allowed = request.form.get("allowed", "true").lower() in ("true", "1", "yes")

    existing = GroupModelAccess.query.filter_by(group_id=gid, model_config_id=model_config_id).first()
    if existing:
        existing.allowed = allowed
    else:
        db.session.add(GroupModelAccess(group_id=gid, model_config_id=model_config_id, allowed=allowed))
    db.session.commit()
    return redirect(url_for("admin.group_detail", gid=gid))


@admin_bp.route("/groups/<int:gid>/access/<int:amid>/delete", methods=["POST"])
@admin_required
def delete_group_access(gid, amid):
    access = GroupModelAccess.query.get_or_404(amid)
    if access.group_id != gid:
        abort(404)
    group = Group.query.get_or_404(gid)
    if group.config_managed:
        abort(403)
    db.session.delete(access)
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
            func.coalesce(func.sum(ModelStat.requests), 0),
            func.coalesce(func.sum(ModelStat.input_tokens + ModelStat.output_tokens), 0),
            func.coalesce(func.sum(ModelStat.cost), 0),
        )
        .join(Entity, ModelStat.entity_id == Entity.id)
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
    models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()

    entity_limit = EntityLimit.query.filter_by(entity_id=eid).first()
    entity_balance = EntityBalance.query.filter_by(entity_id=eid).first()
    effective_pool = get_pool_limit(eid)
    entity_model_access = EntityModelAccess.query.filter_by(entity_id=eid).all()

    memberships = GroupMember.query.filter_by(entity_id=eid).all()
    group_details = []
    for m in memberships:
        group = Group.query.get(m.group_id)
        if group and group.active:
            glimit = GroupLimit.query.filter_by(group_id=group.id).first()
            gaccess = GroupModelAccess.query.filter_by(group_id=group.id).all()
            group_details.append((m, group, glimit, gaccess))

    return render_template(
        "admin/user_limits.html",
        entity=entity,
        models=models,
        entity_limit=entity_limit,
        entity_balance=entity_balance,
        effective_pool=effective_pool,
        entity_model_access=entity_model_access,
        group_details=group_details,
    )


@admin_bp.route("/users/<int:eid>/reset-tokens", methods=["POST"])
@admin_required
def reset_user_tokens(eid):
    entity = Entity.query.get_or_404(eid)
    pool = get_pool_limit(entity.id)
    if pool is None:
        return jsonify({"error": "No token pool configured"}), 400
    max_tokens, _refresh, starting_tokens = pool
    if max_tokens == -2:
        return jsonify({"error": "User has unlimited tokens"}), 400
    new_balance = max(starting_tokens, max_tokens)
    balance = EntityBalance.query.filter_by(entity_id=eid).first()
    if balance:
        balance.tokens_left = new_balance
        balance.last_refill_at = datetime.utcnow()
    else:
        balance = EntityBalance(
            entity_id=eid, tokens_left=new_balance, last_refill_at=datetime.utcnow()
        )
        db.session.add(balance)
    db.session.commit()
    return jsonify({"tokens_available": new_balance})


@admin_bp.route("/users/<int:eid>/pool", methods=["POST"])
@admin_required
def upsert_user_pool(eid):
    Entity.query.get_or_404(eid)
    max_tokens = int(request.form.get("max_tokens", 0))
    refresh_tokens = int(request.form.get("refresh_tokens", 0))
    starting_tokens = int(request.form.get("starting_tokens", 0))

    existing = EntityLimit.query.filter_by(entity_id=eid).first()
    if existing:
        if existing.config_managed:
            abort(403)
        existing.max_tokens = max_tokens
        existing.refresh_tokens = refresh_tokens
        existing.starting_tokens = starting_tokens
    else:
        db.session.add(EntityLimit(
            entity_id=eid,
            max_tokens=max_tokens,
            refresh_tokens=refresh_tokens,
            starting_tokens=starting_tokens,
        ))
    db.session.commit()
    return redirect(url_for("admin.user_limits", eid=eid))


@admin_bp.route("/users/<int:eid>/pool/delete", methods=["POST"])
@admin_required
def delete_user_pool(eid):
    limit = EntityLimit.query.filter_by(entity_id=eid).first()
    if limit:
        if limit.config_managed:
            abort(403)
        db.session.delete(limit)
        db.session.commit()
    return redirect(url_for("admin.user_limits", eid=eid))


@admin_bp.route("/users/<int:eid>/access", methods=["POST"])
@admin_required
def upsert_user_access(eid):
    Entity.query.get_or_404(eid)
    model_config_id = int(request.form.get("model_config_id"))
    allowed = request.form.get("allowed", "true").lower() in ("true", "1", "yes")

    existing = EntityModelAccess.query.filter_by(entity_id=eid, model_config_id=model_config_id).first()
    if existing:
        existing.allowed = allowed
    else:
        db.session.add(EntityModelAccess(entity_id=eid, model_config_id=model_config_id, allowed=allowed))
    db.session.commit()
    return redirect(url_for("admin.user_limits", eid=eid))


@admin_bp.route("/users/<int:eid>/access/<int:amid>/delete", methods=["POST"])
@admin_required
def delete_user_access(eid, amid):
    access = EntityModelAccess.query.get_or_404(amid)
    if access.entity_id != eid:
        abort(404)
    db.session.delete(access)
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
            ModelStat.entity_id,
            func.coalesce(func.sum(ModelStat.requests), 0).label("requests"),
            func.coalesce(func.sum(ModelStat.input_tokens + ModelStat.output_tokens), 0).label("tokens_used"),
            func.coalesce(func.sum(ModelStat.cost), 0).label("cost"),
        )
        .group_by(ModelStat.entity_id)
        .subquery()
    )

    balance_sq = (
        db.session.query(
            EntityBalance.entity_id,
            EntityBalance.tokens_left.label("tokens_available"),
        )
        .subquery()
    )

    unlimited_sq = (
        db.session.query(EntityLimit.entity_id)
        .filter(EntityLimit.max_tokens == -2)
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


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

_PERIODS = {
    "week":  {"offset": timedelta(days=7),   "bucket": "1 day",   "trunc": "day"},
    "month": {"offset": timedelta(days=30),  "bucket": "1 day",   "trunc": "day"},
    "year":  {"offset": timedelta(days=365), "bucket": "1 week",  "trunc": "week"},
    "all":   {"offset": None,                "bucket": "1 month", "trunc": "month"},
}


def _period_start(period_str):
    """Return UTC datetime for the start of the period, or None for all time."""
    cfg = _PERIODS.get(period_str, _PERIODS["week"])
    if cfg["offset"] is None:
        return None
    return datetime.utcnow() - cfg["offset"]


def _period_bucket(period_str):
    cfg = _PERIODS.get(period_str, _PERIODS["week"])
    return cfg["bucket"], cfg["trunc"]


@admin_bp.route("/analytics")
@admin_required
def analytics():
    return render_template("admin/analytics.html")


@admin_bp.route("/api/analytics/summary")
@admin_required
def analytics_summary():
    period = request.args.get("period", "week")
    start = _period_start(period)

    if start is not None:
        row = db.session.execute(text("""
            SELECT
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COALESCE(SUM(cost), 0.0)
            FROM request_counts_hourly
            WHERE bucket >= :start
        """), {"start": start}).one()
        new_users = db.session.query(func.count(Entity.id)).filter(
            Entity.entity_type == "user",
            Entity.created_at >= start,
        ).scalar()
    else:
        row = db.session.execute(text("""
            SELECT
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COALESCE(SUM(cost), 0.0)
            FROM request_counts_hourly
        """)).one()
        new_users = db.session.query(func.count(Entity.id)).filter(
            Entity.entity_type == "user",
        ).scalar()

    return jsonify({
        "requests": int(row[0]),
        "tokens": int(row[1]),
        "cost": float(row[2]),
        "new_users": int(new_users),
    })


@admin_bp.route("/api/analytics/users/new")
@admin_required
def analytics_users_new():
    period = request.args.get("period", "week")
    start = _period_start(period)
    _, trunc = _period_bucket(period)

    if start is not None:
        rows = db.session.execute(text("""
            SELECT date_trunc(:trunc, created_at) AS period, COUNT(*) AS count
            FROM entities
            WHERE entity_type = 'user' AND created_at >= :start
            GROUP BY 1 ORDER BY 1
        """), {"trunc": trunc, "start": start}).all()
    else:
        rows = db.session.execute(text("""
            SELECT date_trunc(:trunc, created_at) AS period, COUNT(*) AS count
            FROM entities
            WHERE entity_type = 'user'
            GROUP BY 1 ORDER BY 1
        """), {"trunc": trunc}).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@admin_bp.route("/api/analytics/users/cumulative")
@admin_required
def analytics_users_cumulative():
    period = request.args.get("period", "week")
    start = _period_start(period)
    _, trunc = _period_bucket(period)

    if start is not None:
        rows = db.session.execute(text("""
            WITH buckets AS (
                SELECT date_trunc(:trunc, created_at) AS period, COUNT(*) AS new_count
                FROM entities
                WHERE entity_type = 'user' AND created_at >= :start
                GROUP BY 1
            )
            SELECT period, SUM(new_count) OVER (ORDER BY period) AS cumulative
            FROM buckets
            ORDER BY period
        """), {"trunc": trunc, "start": start}).all()
    else:
        rows = db.session.execute(text("""
            WITH buckets AS (
                SELECT date_trunc(:trunc, created_at) AS period, COUNT(*) AS new_count
                FROM entities
                WHERE entity_type = 'user'
                GROUP BY 1
            )
            SELECT period, SUM(new_count) OVER (ORDER BY period) AS cumulative
            FROM buckets
            ORDER BY period
        """), {"trunc": trunc}).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@admin_bp.route("/api/analytics/requests")
@admin_required
def analytics_requests():
    period = request.args.get("period", "week")
    start = _period_start(period)
    bucket, _ = _period_bucket(period)

    if start is not None:
        rows = db.session.execute(text(f"""
            SELECT time_bucket('{bucket}', bucket) AS period, SUM(requests) AS count
            FROM request_counts_hourly
            WHERE bucket >= :start
            GROUP BY 1 ORDER BY 1
        """), {"start": start}).all()
    else:
        rows = db.session.execute(text(f"""
            SELECT time_bucket('{bucket}', bucket) AS period, SUM(requests) AS count
            FROM request_counts_hourly
            GROUP BY 1 ORDER BY 1
        """)).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@admin_bp.route("/api/analytics/tokens")
@admin_required
def analytics_tokens():
    period = request.args.get("period", "week")
    start = _period_start(period)
    bucket, _ = _period_bucket(period)

    if start is not None:
        rows = db.session.execute(text(f"""
            SELECT time_bucket('{bucket}', bucket) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_counts_hourly
            WHERE bucket >= :start
            GROUP BY 1 ORDER BY 1
        """), {"start": start}).all()
    else:
        rows = db.session.execute(text(f"""
            SELECT time_bucket('{bucket}', bucket) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_counts_hourly
            GROUP BY 1 ORDER BY 1
        """)).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@admin_bp.route("/api/analytics/models")
@admin_required
def analytics_models():
    period = request.args.get("period", "week")
    start = _period_start(period)

    if start is not None:
        rows = db.session.execute(text("""
            SELECT mc.model_name, SUM(rch.requests) AS requests
            FROM request_counts_hourly rch
            JOIN model_configs mc ON rch.model_config_id = mc.id
            WHERE rch.bucket >= :start
            GROUP BY mc.model_name
            ORDER BY requests DESC
        """), {"start": start}).all()
    else:
        rows = db.session.execute(text("""
            SELECT mc.model_name, SUM(rch.requests) AS requests
            FROM request_counts_hourly rch
            JOIN model_configs mc ON rch.model_config_id = mc.id
            GROUP BY mc.model_name
            ORDER BY requests DESC
        """)).all()

    return jsonify([{"model": r[0], "requests": int(r[1])} for r in rows])


@admin_bp.route("/api/analytics/heatmap")
@admin_required
def analytics_heatmap():
    period = request.args.get("period", "week")
    start = _period_start(period)

    if start is not None:
        rows = db.session.execute(text("""
            SELECT
                EXTRACT(DOW FROM bucket)  AS dow,
                EXTRACT(HOUR FROM bucket) AS hour,
                SUM(requests) AS count
            FROM request_counts_hourly
            WHERE bucket >= :start
            GROUP BY 1, 2
        """), {"start": start}).all()
    else:
        rows = db.session.execute(text("""
            SELECT
                EXTRACT(DOW FROM bucket)  AS dow,
                EXTRACT(HOUR FROM bucket) AS hour,
                SUM(requests) AS count
            FROM request_counts_hourly
            GROUP BY 1, 2
        """)).all()

    return jsonify([{"dow": int(r[0]), "hour": int(r[1]), "count": int(r[2])} for r in rows])
