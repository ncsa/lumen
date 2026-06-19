import os
import shutil
import tempfile

import yaml
from http import HTTPStatus

from flask import Blueprint, current_app, render_template, request, redirect, url_for, jsonify, session
from sqlalchemy import func, case, select

from lumen.blueprints.profile.routes import _entity_groups, _get_profile_data, _gravatar_url
from lumen.decorators import admin_required
from lumen.services.config_watcher import RESTART_REQUIRED
from lumen.extensions import db
from lumen.timeutils import utcnow
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.services.llm import get_model_access_status, get_model_status, get_pool_limit, has_model_consent

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Sentinel for "unlimited" sort key: NULL coins_left rows sort last by mapping to BIGINT_MAX.
# -2 is the canonical "unlimited" value; this encodes it as a sortable integer column.
_BIGINT_MAX = 9223372036854775807
# Must match the options rendered by the frontend per-page selector.
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
        return jsonify({"error": "No coin pool configured"}), HTTPStatus.BAD_REQUEST
    max_coins, _refresh, starting_coins = pool
    if max_coins == -2:
        return jsonify({"error": "User has unlimited coins"}), HTTPStatus.BAD_REQUEST
    new_balance = starting_coins
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=eid)).scalar_one_or_none()
    if balance:
        balance.coins_left = new_balance
        balance.last_refill_at = utcnow()
    else:
        balance = EntityBalance(
            entity_id=eid, coins_left=new_balance, last_refill_at=utcnow()
        )
        db.session.add(balance)
    db.session.commit()
    return jsonify({"coins_available": new_balance})


@admin_bp.route("/users/<int:eid>/profile")
@admin_required
def user_profile(eid):
    entity = db.get_or_404(Entity, eid)
    data = _get_profile_data(eid)
    return render_template(
        "profile.html",
        **data,
        viewing_user=entity,
        profile_entity=entity,
        gravatar_url=_gravatar_url(entity.email, size=230),
        profile_groups=_entity_groups(eid),
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


@admin_bp.route("/analytics")
@admin_required
def analytics():
    return redirect(url_for("profile.usage"))


@admin_bp.route("/config")
@admin_required
def config_editor():
    config_path = current_app.config["CONFIG_YAML"]
    config_readonly = not os.access(config_path, os.W_OK)
    return render_template(
        "admin/config.html",
        current_email=session.get("entity_email", ""),
        restart_required=RESTART_REQUIRED,
        config_readonly=config_readonly,
    )


@admin_bp.route("/api/config")
@admin_required
def config_api_get():
    config_path = current_app.config["CONFIG_YAML"]
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except OSError as e:
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR
    return jsonify(data)


@admin_bp.route("/api/sync_model", methods=["POST"])
@admin_required
def sync_model_api():
    from lumen.services.model_sync import sync_model
    model_def = request.get_json(force=True, silent=True)
    if not isinstance(model_def, dict):
        return jsonify({"error": "Expected a JSON object"}), HTTPStatus.BAD_REQUEST
    result = sync_model(model_def)
    return jsonify(result)


@admin_bp.route("/api/config", methods=["POST"])
@admin_required
def config_api_post():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid payload — expected a JSON object"}), HTTPStatus.BAD_REQUEST
    config_path = current_app.config["CONFIG_YAML"]
    try:
        parts = [
            yaml.dump({k: v}, Dumper=yaml.SafeDumper, default_flow_style=False, allow_unicode=True, sort_keys=False)
            for k, v in data.items()
        ]
        fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(parts))
            # Back up the current config before overwriting so a partial or
            # malformed save can be recovered from <config>.bak.
            if os.path.exists(config_path):
                shutil.copy2(config_path, config_path + ".bak")
            shutil.copyfile(tmp_path, config_path)
        finally:
            os.unlink(tmp_path)
    except OSError as e:
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR
    return jsonify({"ok": True})
