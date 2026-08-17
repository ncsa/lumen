import os
from decimal import Decimal, InvalidOperation

import yaml
from http import HTTPStatus

from flask import Blueprint, current_app, render_template, request, redirect, url_for, jsonify, session
from sqlalchemy import func, case, delete, select

from lumen.blueprints.profile.routes import _entity_groups, _get_profile_data, _gravatar_url
from lumen.commands import write_config_yaml
from lumen.decorators import admin_required
from lumen.services.config_watcher import (
    RESTART_REQUIRED,
    _find_unrestorable_masks,
    mask_config_secrets,
    restore_config_secrets,
)
from lumen.extensions import db
from lumen.timeutils import utcnow
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.model_config import ModelConfig
from lumen.models.model_group_access import ModelGroupAccess
from lumen.services.llm import get_model_access_status, get_model_status, get_pool_limit, has_model_consent

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Sentinel for "unlimited" sort key: NULL coins_left rows sort last by mapping to BIGINT_MAX.
# -2 is the canonical "unlimited" value; this encodes it as a sortable integer column.
_BIGINT_MAX = 9223372036854775807
# Must match the options rendered by the frontend per-page selector.
_VALID_PER_PAGE = {25, 50, 100, 200}


# Coin fields must fit Numeric(12, 6): 6 integer digits.
_COIN_MAX_MAGNITUDE = Decimal(10) ** 6


def apply_coin_pool_edit(entity_id, data):
    """Validate max_coins/refresh_coins from a UI edit payload and upsert the
    entity's coin pool as UI-managed (config_managed=False).

    Keys absent from the payload leave the pool untouched. A blank
    (empty/null) max_coins clears the entity's own pool — the row is deleted
    so the entity falls back to its group/default pool. A blank refresh_coins
    leaves the current rate unchanged (0 when creating a new pool). Lowering
    max_coins clamps the entity's current balance. The UI exposes no separate
    starting value, so starting_coins (what reset-tokens refills to) tracks
    max_coins whenever a finite max is set. Returns an error message, or None
    on success (caller commits).
    """
    present = {k: data[k] for k in ("max_coins", "refresh_coins") if k in data}
    if not present:
        return None

    def _blank(v):
        return v is None or v == ""

    if "max_coins" in present and _blank(present["max_coins"]):
        limit = db.session.execute(
            select(EntityLimit).filter_by(entity_id=entity_id)
        ).scalar_one_or_none()
        if limit is not None:
            db.session.delete(limit)
        return None

    values = {}
    for key, raw in present.items():
        if _blank(raw):
            continue
        try:
            values[key] = Decimal(str(raw))
        except InvalidOperation:
            return f"{key} must be a number"
    if not values:
        return None
    max_coins = values.get("max_coins")
    refresh_coins = values.get("refresh_coins")
    if max_coins is not None and max_coins != -2 and max_coins < 0:
        return "max_coins must be -2 (unlimited) or >= 0"
    if refresh_coins is not None and refresh_coins < 0:
        return "refresh_coins must be >= 0"
    if any(v.copy_abs() >= _COIN_MAX_MAGNITUDE for v in values.values()):
        return "coin values must be less than 1,000,000"

    limit = db.session.execute(
        select(EntityLimit).filter_by(entity_id=entity_id)
    ).scalar_one_or_none()
    if limit is None:
        if max_coins is None:
            return "max_coins is required when no coin pool exists yet"
        limit = EntityLimit(entity_id=entity_id, starting_coins=0)
        db.session.add(limit)
    if max_coins is not None:
        limit.max_coins = max_coins
        if max_coins >= 0:
            limit.starting_coins = max_coins
    if refresh_coins is not None:
        limit.refresh_coins = refresh_coins
    limit.config_managed = False

    if max_coins is not None and max_coins >= 0:
        balance = db.session.execute(
            select(EntityBalance).filter_by(entity_id=entity_id)
        ).scalar_one_or_none()
        if balance and balance.coins_left > max_coins:
            balance.coins_left = max_coins
    return None


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


@admin_bp.route("/users/<int:eid>", methods=["PATCH"])
@admin_required
def update_user(eid):
    """Edit a user's active flag and coin pool from the profile Edit dialog."""
    entity = db.first_or_404(select(Entity).filter_by(id=eid, entity_type="user"))
    data = request.get_json() or {}
    error = apply_coin_pool_edit(eid, data)
    if error:
        return jsonify({"error": error}), HTTPStatus.BAD_REQUEST
    if "active" in data:
        entity.active = bool(data["active"])
    db.session.commit()
    return jsonify({"ok": True, "active": entity.active})


@admin_bp.route("/users/<int:eid>/reset-tokens", methods=["POST"])
@admin_bp.route("/entities/<int:eid>/reset-tokens", methods=["POST"])
@admin_required
def reset_user_tokens(eid):
    """Refill an entity's (user or project) balance to its starting coins."""
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
    # The user's own limit row (not an inherited group/default pool), used to
    # prefill the edit dialog without materializing inherited values.
    user_limit = db.session.execute(
        select(EntityLimit).filter_by(entity_id=eid)
    ).scalar_one_or_none()
    return render_template(
        "profile.html",
        **data,
        viewing_user=entity,
        profile_entity=entity,
        gravatar_url=_gravatar_url(entity.email, size=230),
        profile_groups=_entity_groups(eid),
        user_limit=user_limit,
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
    sort = request.args.get("sort", "last_used")
    order = request.args.get("order", "desc")
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

    # The user's own coin limit (None = inherited pool), for the edit dialog.
    page_ids = [entity.id for entity, *_ in rows]
    limits = {
        lim.entity_id: lim
        for lim in db.session.execute(
            select(EntityLimit).where(EntityLimit.entity_id.in_(page_ids))
        ).scalars().all()
    } if page_ids else {}

    return jsonify({
        "users": [
            {
                "id": entity.id,
                "name": entity.name,
                "active": entity.active,
                "max_coins": float(limits[entity.id].max_coins) if entity.id in limits else None,
                "refresh_coins": float(limits[entity.id].refresh_coins) if entity.id in limits else None,
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
    editor_enabled = current_app.config.get("CONFIG_EDITOR", True)
    # Read-only when the editor is disabled by config, or the file is not writable.
    config_readonly = (not editor_enabled) or (not os.access(config_path, os.W_OK))
    return render_template(
        "admin/config.html",
        current_email=session.get("entity_email", ""),
        restart_required=RESTART_REQUIRED,
        config_readonly=config_readonly,
        editor_disabled=not editor_enabled,
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
    mask_config_secrets(data)
    return jsonify(data)


@admin_bp.route("/api/users/search")
@admin_required
def users_search_api():
    """Typeahead search over existing user entities by name or email (config editor)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"users": []})
    stmt = (
        select(Entity)
        .where(
            Entity.entity_type == "user",
            db.or_(Entity.email.ilike(f"%{q}%"), Entity.name.ilike(f"%{q}%")),
        )
        .order_by(Entity.name)
        .limit(10)
    )
    users = db.session.execute(stmt).scalars().all()
    return jsonify({"users": [{"name": u.name, "email": u.email} for u in users if u.email]})


def _model_access_payload(mc):
    """JSON payload describing a model's owner and group grants for the Access dialog."""
    owner = db.session.get(Entity, mc.owner_entity_id) if mc.owner_entity_id else None
    granted_group_ids = [
        g.group_id for g in db.session.execute(
            select(ModelGroupAccess).filter_by(model_config_id=mc.id)
        ).scalars().all()
    ]
    # Grants through inactive groups have no effect, so offer only active
    # groups — plus any inactive group already granted, so the stale grant
    # stays visible and removable.
    groups = db.session.execute(
        select(Group).where(
            db.or_(Group.active == True, Group.id.in_(granted_group_ids))  # noqa: E712
        ).order_by(Group.name)
    ).scalars().all()
    return {
        "owner": {"id": owner.id, "name": owner.name, "email": owner.email} if owner else None,
        "granted_group_ids": granted_group_ids,
        "groups": [{"id": g.id, "name": g.name, "active": g.active} for g in groups],
    }


def apply_model_access_edit(mc, data):
    """Set a model's owner and group grants from an Access dialog payload.

    owner_email blank/null makes the model public and drops all its grants
    (a public model has no grants). group_ids replaces the grant set and is
    only honored when the model has an owner. Returns an error message, or
    None on success (caller commits).
    """
    # Validate everything before mutating so an error response leaves no partial edit.
    new_owner_id = mc.owner_entity_id
    if "owner_email" in data:
        owner_email = (data.get("owner_email") or "").strip()
        if not owner_email:
            new_owner_id = None
        else:
            owner = db.session.execute(
                select(Entity).where(
                    Entity.entity_type == "user",
                    db.func.lower(Entity.email) == owner_email.lower(),
                )
            ).scalar_one_or_none()
            if owner is None:
                return "no user with that email"
            new_owner_id = owner.id

    if new_owner_id is None:
        mc.owner_entity_id = None
        db.session.execute(delete(ModelGroupAccess).where(ModelGroupAccess.model_config_id == mc.id))
        return None

    if "group_ids" in data:
        raw_ids = data.get("group_ids") or []
        if not isinstance(raw_ids, list) or not all(isinstance(g, int) for g in raw_ids):
            return "group_ids must be a list of group ids"
        desired = set(raw_ids)
        existing_ids = {
            g.id for g in db.session.execute(select(Group).where(Group.id.in_(desired))).scalars().all()
        } if desired else set()
        unknown = desired - existing_ids
        if unknown:
            return "unknown group id(s): " + ", ".join(str(g) for g in sorted(unknown))
        current = {
            row.group_id: row for row in db.session.execute(
                select(ModelGroupAccess).filter_by(model_config_id=mc.id)
            ).scalars().all()
        }
        for group_id, row in current.items():
            if group_id not in desired:
                db.session.delete(row)
        for group_id in desired:
            if group_id not in current:
                db.session.add(ModelGroupAccess(model_config_id=mc.id, group_id=group_id))
    mc.owner_entity_id = new_owner_id
    return None


@admin_bp.route("/api/models/<int:mid>/access")
@admin_required
def model_access_api_get(mid):
    """Owner + group grants for the model detail Access dialog."""
    mc = db.get_or_404(ModelConfig, mid)
    return jsonify(_model_access_payload(mc))


@admin_bp.route("/api/models/<int:mid>/access", methods=["PATCH"])
@admin_required
def model_access_api_patch(mid):
    """Update a model's owner and group grants from the Access dialog."""
    mc = db.get_or_404(ModelConfig, mid)
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), HTTPStatus.BAD_REQUEST
    error = apply_model_access_edit(mc, data)
    if error:
        return jsonify({"error": error}), HTTPStatus.BAD_REQUEST
    db.session.commit()
    return jsonify(_model_access_payload(mc))


@admin_bp.route("/api/sync_model", methods=["POST"])
@admin_required
def sync_model_api():
    from lumen.services.model_sync import sync_model
    from lumen.services.config_watcher import MASK
    model_def = request.get_json(force=True, silent=True)
    if not isinstance(model_def, dict):
        return jsonify({"error": "Expected a JSON object"}), HTTPStatus.BAD_REQUEST
    # The browser sends endpoint api_keys masked as MASK; the sync probe needs
    # the real key to authenticate against the endpoint. Restore from on-disk
    # config by matching model name + endpoint URL before probing.
    config_path = current_app.config["CONFIG_YAML"]
    try:
        with open(config_path) as f:
            on_disk = yaml.safe_load(f) or {}
    except OSError:
        on_disk = {}
    disk_model = next((m for m in on_disk.get("models", [])
                       if isinstance(m, dict) and m.get("name") == model_def.get("name")), None)
    if disk_model:
        disk_keys = {(ep.get("url", "").rstrip("/") or ""): ep.get("api_key")
                     for ep in disk_model.get("endpoints", []) if isinstance(ep, dict)}
        for ep in model_def.get("endpoints", []):
            if ep.get("api_key") == MASK:
                restored = disk_keys.get((ep.get("url", "") or "").rstrip("/"))
                if restored:
                    ep["api_key"] = restored
    result = sync_model(model_def)
    return jsonify(result)


@admin_bp.route("/api/config", methods=["POST"])
@admin_required
def config_api_post():
    if not current_app.config.get("CONFIG_EDITOR", True):
        return jsonify({"error": "Config editor is disabled"}), HTTPStatus.FORBIDDEN
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid payload — expected a JSON object"}), HTTPStatus.BAD_REQUEST
    config_path = current_app.config["CONFIG_YAML"]
    # Re-read on-disk config so sentinel-masked secrets are preserved on save.
    # Missing file (fresh install / briefly absent path) → nothing to restore from.
    try:
        with open(config_path) as f:
            on_disk = yaml.safe_load(f) or {}
    except OSError:
        on_disk = {}
    restore_config_secrets(data, on_disk)
    # Reject any MASK that could not be restored (e.g. model/url changed) so the
    # literal "********" never reaches config.yaml.  Names the offending field(s).
    unrestorable = _find_unrestorable_masks(data)
    if unrestorable:
        return jsonify({
            "error": "Could not restore masked secret(s) — the model/url may have "
                     "changed. Re-enter: " + ", ".join(unrestorable),
        }), HTTPStatus.BAD_REQUEST
    try:
        write_config_yaml(config_path, data)
    except OSError as e:
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR
    return jsonify({"ok": True})
