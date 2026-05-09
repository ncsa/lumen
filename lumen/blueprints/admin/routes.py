from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request, redirect, url_for, abort, jsonify
from sqlalchemy import func, case, select, text

from lumen.decorators import admin_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.services.llm import get_pool_limit

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_BIGINT_MAX = 9223372036854775807
_VALID_PER_PAGE = {25, 50, 100, 200}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@admin_bp.route("/users")
@admin_required
def users():
    total_users = db.session.scalar(
        select(func.count()).select_from(Entity).filter_by(entity_type="user")
    )
    stats = db.session.execute(
        select(
            func.coalesce(func.sum(EntityStat.requests), 0),
            func.coalesce(func.sum(EntityStat.input_tokens + EntityStat.output_tokens), 0),
            func.coalesce(func.sum(EntityStat.cost), 0),
        )
        .join(Entity, EntityStat.entity_id == Entity.id)
        .where(Entity.entity_type == "user")
    ).one()
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
    entity = db.get_or_404(Entity, eid)
    entity.active = not entity.active
    db.session.commit()
    return jsonify({"active": entity.active})


@admin_bp.route("/users/<int:eid>/reset-tokens", methods=["POST"])
@admin_required
def reset_user_tokens(eid):
    entity = db.get_or_404(Entity, eid)
    pool = get_pool_limit(entity.id)
    if pool is None:
        return jsonify({"error": "No coin pool configured"}), 400
    max_coins, _refresh, starting_coins = pool
    if max_coins == -2:
        return jsonify({"error": "User has unlimited coins"}), 400
    new_balance = max(starting_coins, max_coins)
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=eid)).scalar_one_or_none()
    if balance:
        balance.coins_left = new_balance
        balance.last_refill_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        balance = EntityBalance(
            entity_id=eid, coins_left=new_balance, last_refill_at=datetime.now(timezone.utc).replace(tzinfo=None)
        )
        db.session.add(balance)
    db.session.commit()
    return jsonify({"coins_available": new_balance})


@admin_bp.route("/users/<int:eid>/usage")
@admin_required
def user_usage(eid):
    from lumen.blueprints.usage.routes import _get_usage_data
    from lumen.services.llm import get_model_access_status, get_model_status, has_model_consent
    entity = db.get_or_404(Entity, eid)
    data = _get_usage_data(eid)
    all_models = db.session.execute(select(ModelConfig).order_by(ModelConfig.model_name)).scalars().all()
    usage_by_model = {u["model_name"]: u for u in data.get("model_usage", [])}
    model_access_list = []
    for mc in all_models:
        access_status = get_model_access_status(eid, mc.id)
        consented = has_model_consent(eid, mc.id) if access_status == "graylist" else None
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
        "usage.html",
        **data,
        model_access_list=model_access_list,
        viewing_user=entity,
    )


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
            func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0).label("tokens_used"),
            func.coalesce(EntityStat.cost, 0).label("cost"),
            coins_avail_sort.label("coins_available"),
            EntityStat.last_used_at.label("last_used_at"),
        )
        .where(Entity.entity_type == "user")
        .outerjoin(EntityStat, Entity.id == EntityStat.entity_id)
        .outerjoin(balance_sq, Entity.id == balance_sq.c.entity_id)
        .outerjoin(unlimited_sq, Entity.id == unlimited_sq.c.entity_id)
    )

    if search:
        like = f"%{search}%"
        stmt = stmt.where(Entity.name.ilike(like) | Entity.email.ilike(like))

    sort_col = {
        "name": Entity.name,
        "active": Entity.active,
        "joined": Entity.created_at,
        "last_used": EntityStat.last_used_at,
        "requests": func.coalesce(EntityStat.requests, 0),
        "tokens_used": func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0),
        "cost": func.coalesce(EntityStat.cost, 0),
        "coins_available": coins_avail_sort,
    }.get(sort, Entity.name)

    direction = sort_col.desc().nullslast() if order == "desc" else sort_col.asc().nullslast()
    stmt = stmt.order_by(direction)

    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.session.execute(stmt.offset((page - 1) * per_page).limit(per_page)).all()

    return jsonify({
        "users": [
            {
                "id": entity.id,
                "name": entity.name,
                "active": entity.active,
                "joined": entity.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if entity.created_at else None,
                "last_used": last_used_at.strftime("%Y-%m-%dT%H:%M:%SZ") if last_used_at else None,
                "requests": int(requests),
                "tokens_used": int(tokens_used),
                "cost": float(cost),
                "coins_available": -2 if float(coins_available) >= _BIGINT_MAX else float(coins_available),
            }
            for entity, requests, tokens_used, cost, coins_available, last_used_at in rows
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
    return datetime.now(timezone.utc).replace(tzinfo=None) - cfg["offset"]


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
        new_users = db.session.scalar(
            select(func.count(Entity.id)).where(
                Entity.entity_type == "user",
                Entity.created_at >= start,
            )
        )
    else:
        row = db.session.execute(text("""
            SELECT
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COALESCE(SUM(cost), 0.0)
            FROM request_counts_hourly
        """)).one()
        new_users = db.session.scalar(
            select(func.count(Entity.id)).where(Entity.entity_type == "user")
        )

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
