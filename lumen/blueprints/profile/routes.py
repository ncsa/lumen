import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from flask import Blueprint, current_app, redirect, render_template, request, jsonify, session, url_for, abort
from sqlalchemy import func, select, text

from lumen.decorators import login_required, is_admin as _is_admin
from lumen.extensions import db
from lumen.timeutils import utcnow
from lumen.models.api_key import APIKey
from lumen.models.conversation import Conversation
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.models.entity_stat import EntityStat
from lumen.services.crypto import hash_api_key
from lumen.services.llm import bulk_model_access_info, get_pool_limit, get_model_access_status, has_model_consent

profile_bp = Blueprint("profile", __name__)


def _gravatar_url(email: str, size: int = 80) -> str:
    h = hashlib.md5((email or "").strip().lower().encode()).hexdigest()
    return f"https://www.gravatar.com/avatar/{h}?s={size}&d=mp"


def _entity_groups(eid: int) -> list:
    return db.session.execute(
        select(Group).join(GroupMember, Group.id == GroupMember.group_id)
        .where(GroupMember.entity_id == eid, Group.name != "default")
        .order_by(Group.name)
    ).scalars().all()


def _endpoint_status(eps: list) -> str:
    """Map a model's endpoints to a health status: down / degraded / ok."""
    healthy = sum(1 for e in eps if e.healthy)
    if not eps or healthy == 0:
        return "down"
    if healthy < len(eps):
        return "degraded"
    return "ok"


def _fetch_model_context(eid: int):
    """Fetch all models, their endpoints, and bulk access/consent info once.

    Shared by _build_model_usage and _build_model_access_list so a profile page
    resolves model access and endpoints a single time instead of twice.
    """
    all_models = db.session.execute(select(ModelConfig).order_by(ModelConfig.model_name)).scalars().all()
    model_ids = [mc.id for mc in all_models]
    eps_by_model: dict = {}
    if model_ids:
        for ep in db.session.execute(
            select(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(model_ids))
        ).scalars().all():
            eps_by_model.setdefault(ep.model_config_id, []).append(ep)
    access_statuses, consent_map = bulk_model_access_info(eid, model_ids)
    return all_models, eps_by_model, access_statuses, consent_map


def _build_model_access_list(usage_by_model, all_models, eps_by_model, access_statuses, consent_map) -> list:
    """Merge access status, model health, and usage stats for every model."""
    default_ack = current_app.config.get("MODEL_DEFAULTS", {}).get("ack_message")
    result = []
    for mc in all_models:
        access_status = access_statuses.get(mc.id, "allowed")
        consented = (mc.id in consent_map) if access_status == "needs_ack" else None
        u = usage_by_model.get(mc.model_name, {})
        model_status = "disabled" if not mc.active else _endpoint_status(eps_by_model.get(mc.id, []))
        result.append({
            "model_name": mc.model_name,
            "model_url": url_for("models_page.detail", model_name=mc.model_name),
            "notice": (mc.ack_message or default_ack) if access_status == "needs_ack" else None,
            "consent_at": consent_map.get(mc.id) if access_status == "needs_ack" else None,
            "access_status": access_status,
            "consented": consented,
            "model_status": model_status,
            "requests": u.get("requests", 0),
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cost": u.get("cost", 0.0),
            "last_used_at": u.get("last_used_at"),
        })
    return result


def _fetch_chat_stats(eid: int):
    chat_agg = db.session.execute(
        select(
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        ).filter_by(entity_id=eid, source="chat")
    ).one()
    conversation_count = db.session.scalar(
        select(func.count(Conversation.id)).filter_by(entity_id=eid)
    ) or 0
    return chat_agg, conversation_count


def _build_model_usage(eid: int, all_models, eps_by_model, access_statuses):
    active_ids = {mc.id for mc in all_models if mc.active}
    accessible_model_ids = {mid for mid in active_ids if access_statuses.get(mid) != "blocked"}

    usage_rows = db.session.execute(
        select(
            ModelStat.model_config_id,
            func.sum(ModelStat.requests),
            func.sum(ModelStat.input_tokens),
            func.sum(ModelStat.output_tokens),
            func.sum(ModelStat.cost),
            func.max(ModelStat.last_used_at),
        )
        .where(ModelStat.entity_id == eid)
        .group_by(ModelStat.model_config_id)
    ).all()
    usage_by_id = {r[0]: r for r in usage_rows}

    # all_models is already ordered by name; keep accessible-active models and any
    # model the entity has usage on (which may now be inactive).
    models_to_show_ids = accessible_model_ids | set(usage_by_id)
    all_relevant_models = [mc for mc in all_models if mc.id in models_to_show_ids]

    model_usage = []
    for mc in all_relevant_models:
        u = usage_by_id.get(mc.id)
        has_access = mc.id in accessible_model_ids
        status = "disabled" if not has_access else _endpoint_status(eps_by_model.get(mc.id, []))
        model_usage.append({
            "model_name": mc.model_name,
            "requests": int(u[1] or 0) if u else 0,
            "input_tokens": int(u[2] or 0) if u else 0,
            "output_tokens": int(u[3] or 0) if u else 0,
            "cost": float(u[4] or 0) if u else 0.0,
            "last_used_at": u[5] if u else None,
            "status": status,
            "disabled": status == "disabled",
        })

    total_tokens_used = sum((r[2] or 0) + (r[3] or 0) for r in usage_rows)
    total_cost = sum(float(r[4] or 0) for r in usage_rows)
    return model_usage, total_tokens_used, total_cost


def _build_coin_pool(eid: int):
    pool = get_pool_limit(eid)
    if pool is None:
        return None
    max_coins, refresh_coins, starting = pool
    balance = db.session.execute(select(EntityBalance).filter_by(entity_id=eid)).scalar_one_or_none()
    coins_left = float(balance.coins_left) if balance else starting
    last_refill_at = balance.last_refill_at if balance else None
    return {
        "coin_limit": max_coins,
        "coins_left": coins_left,
        "coins_per_hour": refresh_coins,
        "next_refill": (last_refill_at + timedelta(hours=1)) if (refresh_coins > 0 and last_refill_at) else None,
    }


def _get_profile_data(eid: int) -> dict:
    chat_agg, conversation_count = _fetch_chat_stats(eid)
    api_keys = db.session.execute(select(APIKey).filter_by(entity_id=eid).order_by(APIKey.created_at)).scalars().all()
    # Fetch model context once and build both the usage list and the access list from it.
    all_models, eps_by_model, access_statuses, consent_map = _fetch_model_context(eid)
    model_usage, total_tokens_used, total_cost = _build_model_usage(eid, all_models, eps_by_model, access_statuses)
    usage_by_model = {u["model_name"]: u for u in model_usage}
    model_access_list = _build_model_access_list(usage_by_model, all_models, eps_by_model, access_statuses, consent_map)
    coin_pool = _build_coin_pool(eid)
    return {
        "chat_agg": chat_agg,
        "conversation_count": conversation_count,
        "api_keys": api_keys,
        "model_usage": model_usage,
        "model_access_list": model_access_list,
        "coin_pool": coin_pool,
        "status": {"total_tokens_used": total_tokens_used, "total_cost": total_cost},
    }


@profile_bp.route("/profile")
@login_required
def index():
    entity_id = session["entity_id"]
    data = _get_profile_data(entity_id)

    profile_entity = db.session.get(Entity, entity_id)
    return render_template(
        "profile.html", **data,
        profile_entity=profile_entity,
        gravatar_url=_gravatar_url(profile_entity.email if profile_entity else "", size=230),
        profile_groups=_entity_groups(entity_id),
    )


@profile_bp.route("/profile/project/<int:sid>")
@login_required
def project_profile_page(sid):
    return redirect(url_for("projects.detail", sid=sid), HTTPStatus.MOVED_PERMANENTLY)


@profile_bp.route("/profile/keys/generate")
@login_required
def generate_key():
    key = "sk_" + secrets.token_urlsafe(32)
    return jsonify({"key": key})


@profile_bp.route("/profile/keys", methods=["POST"])
@login_required
def create_key():
    entity_id = session["entity_id"]
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    key = (data.get("key") or "").strip()

    if not key or not key.startswith("sk_"):
        return jsonify({"error": "Invalid key"}), HTTPStatus.BAD_REQUEST

    key_hash = hash_api_key(key)
    if db.session.execute(select(APIKey).filter_by(key_hash=key_hash)).scalar_one_or_none():
        return jsonify({"error": "Key already exists"}), HTTPStatus.CONFLICT

    api_key = APIKey(
        entity_id=entity_id,
        name=name or "Unnamed Key",
        key_hash=key_hash,
        key_hint=f"{key[:7]}...{key[-4:]}",
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({"id": api_key.id, "name": api_key.name, "key": key}), HTTPStatus.CREATED


@profile_bp.route("/profile/keys/<int:kid>", methods=["DELETE"])
@login_required
def delete_key(kid):
    entity_id = session["entity_id"]
    api_key = db.get_or_404(APIKey, kid)

    if api_key.entity_id != entity_id:
        return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN

    db.session.delete(api_key)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@profile_bp.route("/profile/consent/<path:model_name>", methods=["POST"])
@login_required
def user_consent(model_name):
    entity_id = session["entity_id"]
    config = db.first_or_404(select(ModelConfig).where(ModelConfig.model_name == model_name, ModelConfig.active))

    if get_model_access_status(entity_id, config.id) != "needs_ack":
        return jsonify({"error": "Model does not require acknowledgement for this user"}), HTTPStatus.BAD_REQUEST

    if not has_model_consent(entity_id, config.id):
        db.session.add(EntityModelConsent(
            entity_id=entity_id,
            model_config_id=config.id,
            consented_at=utcnow(),
        ))
        db.session.commit()

    return jsonify({"ok": True}), HTTPStatus.OK


# ---------------------------------------------------------------------------
# Usage / Analytics
# ---------------------------------------------------------------------------

_USAGE_PERIODS = {
    "week":  {"offset": timedelta(days=7),   "bucket": "1 day",   "trunc": "day"},
    "month": {"offset": timedelta(days=30),  "bucket": "1 day",   "trunc": "day"},
    "year":  {"offset": timedelta(days=365), "bucket": "1 week",  "trunc": "week"},
    "all":   {"offset": None,                "bucket": "1 month", "trunc": "month"},
}
_VALID_BUCKETS = frozenset(cfg["bucket"] for cfg in _USAGE_PERIODS.values())
_VALID_TRUNC   = frozenset(cfg["trunc"]  for cfg in _USAGE_PERIODS.values())


def _usage_period_start(period_str):
    cfg = _USAGE_PERIODS.get(period_str, _USAGE_PERIODS["week"])
    if cfg["offset"] is None:
        return None
    return datetime.now(timezone.utc) - cfg["offset"]


def _usage_period_bucket(period_str):
    cfg = _USAGE_PERIODS.get(period_str, _USAGE_PERIODS["week"])
    return cfg["bucket"], cfg["trunc"]


def _usage_entity_id():
    """Return entity_id to filter by.

    Non-admins always see their own data (backend enforcement).
    Admins: entity_id param > mine=1 param > None (all users).
    """
    entity = db.session.get(Entity, session["entity_id"])
    if not _is_admin(entity):
        return session["entity_id"]
    eid = request.args.get("entity_id", type=int)
    if eid:
        return eid
    if request.args.get("mine") == "1":
        return session.get("entity_id")
    return None


@profile_bp.route("/usage")
@login_required
def usage():
    return render_template("usage.html")


@profile_bp.route("/api/usage/summary")
@login_required
def usage_summary():
    if db.engine.dialect.name != "postgresql":
        return jsonify({"requests": 0, "tokens": 0, "cost": 0.0, "new_users": 0, "last_active": None})
    period = request.args.get("period", "week")
    start = _usage_period_start(period)
    eid = _usage_entity_id()

    if eid:
        params = {"eid": eid}
        where = "WHERE entity_id = :eid"
        if start is not None:
            params["start"] = start
            where += " AND time >= :start"
        row = db.session.execute(text(f"""
            SELECT
                COALESCE(COUNT(*), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COALESCE(SUM(cost), 0.0)
            FROM request_logs
            {where}
        """), params).one()
        stat = db.session.execute(
            select(EntityStat).filter_by(entity_id=eid)
        ).scalar_one_or_none()
        last_active = (
            stat.last_used_at.isoformat() + "Z" if stat and stat.last_used_at else None
        )
        new_users = 0
    elif start is not None:
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
        last_active = None
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
        last_active = None

    return jsonify({
        "requests": int(row[0]),
        "tokens": int(row[1]),
        "cost": float(row[2]),
        "new_users": int(new_users),
        "last_active": last_active,
    })


@profile_bp.route("/api/usage/users/new")
@login_required
def usage_users_new():
    if db.engine.dialect.name != "postgresql":
        return jsonify([])
    period = request.args.get("period", "week")
    if period not in _USAGE_PERIODS:
        abort(HTTPStatus.BAD_REQUEST)
    start = _usage_period_start(period)
    _, trunc = _usage_period_bucket(period)
    if trunc not in _VALID_TRUNC:
        abort(HTTPStatus.BAD_REQUEST)

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


@profile_bp.route("/api/usage/users/cumulative")
@login_required
def usage_users_cumulative():
    if db.engine.dialect.name != "postgresql":
        return jsonify([])
    period = request.args.get("period", "week")
    if period not in _USAGE_PERIODS:
        abort(HTTPStatus.BAD_REQUEST)
    start = _usage_period_start(period)
    _, trunc = _usage_period_bucket(period)
    if trunc not in _VALID_TRUNC:
        abort(HTTPStatus.BAD_REQUEST)

    if start is not None:
        rows = db.session.execute(text("""
            WITH baseline AS (
                SELECT COUNT(*) AS prior
                FROM entities
                WHERE entity_type = 'user' AND created_at < :start
            ),
            buckets AS (
                SELECT date_trunc(:trunc, created_at) AS period, COUNT(*) AS new_count
                FROM entities
                WHERE entity_type = 'user' AND created_at >= :start
                GROUP BY 1
            )
            SELECT period,
                   (SELECT prior FROM baseline) + SUM(new_count) OVER (ORDER BY period) AS cumulative
            FROM buckets ORDER BY period
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
            FROM buckets ORDER BY period
        """), {"trunc": trunc}).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@profile_bp.route("/api/usage/requests")
@login_required
def usage_requests():
    if db.engine.dialect.name != "postgresql":
        return jsonify([])
    period = request.args.get("period", "week")
    start = _usage_period_start(period)
    bucket, _ = _usage_period_bucket(period)
    if bucket not in _VALID_BUCKETS:
        abort(HTTPStatus.BAD_REQUEST)
    eid = _usage_entity_id()

    if eid:
        params = {"bucket": bucket, "eid": eid}
        where = "WHERE entity_id = :eid"
        if start is not None:
            params["start"] = start
            where += " AND time >= :start"
        rows = db.session.execute(text(f"""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), time) AS period, COUNT(*) AS count
            FROM request_logs
            {where}
            GROUP BY 1 ORDER BY 1
        """), params).all()
    elif start is not None:
        rows = db.session.execute(text("""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), bucket) AS period, SUM(requests) AS count
            FROM request_counts_hourly
            WHERE bucket >= :start
            GROUP BY 1 ORDER BY 1
        """), {"bucket": bucket, "start": start}).all()
    else:
        rows = db.session.execute(text("""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), bucket) AS period, SUM(requests) AS count
            FROM request_counts_hourly
            GROUP BY 1 ORDER BY 1
        """), {"bucket": bucket}).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@profile_bp.route("/api/usage/tokens")
@login_required
def usage_tokens():
    if db.engine.dialect.name != "postgresql":
        return jsonify([])
    period = request.args.get("period", "week")
    start = _usage_period_start(period)
    bucket, _ = _usage_period_bucket(period)
    if bucket not in _VALID_BUCKETS:
        abort(HTTPStatus.BAD_REQUEST)
    eid = _usage_entity_id()

    if eid:
        params = {"bucket": bucket, "eid": eid}
        where = "WHERE entity_id = :eid"
        if start is not None:
            params["start"] = start
            where += " AND time >= :start"
        rows = db.session.execute(text(f"""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), time) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_logs
            {where}
            GROUP BY 1 ORDER BY 1
        """), params).all()
    elif start is not None:
        rows = db.session.execute(text("""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), bucket) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_counts_hourly
            WHERE bucket >= :start
            GROUP BY 1 ORDER BY 1
        """), {"bucket": bucket, "start": start}).all()
    else:
        rows = db.session.execute(text("""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), bucket) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_counts_hourly
            GROUP BY 1 ORDER BY 1
        """), {"bucket": bucket}).all()

    return jsonify([{"period": r[0].isoformat(), "count": int(r[1])} for r in rows])


@profile_bp.route("/api/usage/models")
@login_required
def usage_models():
    if db.engine.dialect.name != "postgresql":
        return jsonify([])
    period = request.args.get("period", "week")
    start = _usage_period_start(period)
    eid = _usage_entity_id()

    if eid:
        params = {"eid": eid}
        where = "WHERE rl.entity_id = :eid"
        if start is not None:
            params["start"] = start
            where += " AND rl.time >= :start"
        rows = db.session.execute(text(f"""
            SELECT mc.model_name, COUNT(*) AS requests
            FROM request_logs rl
            JOIN model_configs mc ON rl.model_config_id = mc.id
            {where}
            GROUP BY mc.model_name ORDER BY requests DESC
        """), params).all()
    elif start is not None:
        rows = db.session.execute(text("""
            SELECT mc.model_name, SUM(rch.requests) AS requests
            FROM request_counts_hourly rch
            JOIN model_configs mc ON rch.model_config_id = mc.id
            WHERE rch.bucket >= :start
            GROUP BY mc.model_name ORDER BY requests DESC
        """), {"start": start}).all()
    else:
        rows = db.session.execute(text("""
            SELECT mc.model_name, SUM(rch.requests) AS requests
            FROM request_counts_hourly rch
            JOIN model_configs mc ON rch.model_config_id = mc.id
            GROUP BY mc.model_name ORDER BY requests DESC
        """)).all()

    return jsonify([{"model": r[0], "requests": int(r[1])} for r in rows])


@profile_bp.route("/api/usage/heatmap")
@login_required
def usage_heatmap():
    if db.engine.dialect.name != "postgresql":
        return jsonify([])
    period = request.args.get("period", "week")
    start = _usage_period_start(period)
    eid = _usage_entity_id()

    if eid:
        params = {"eid": eid}
        where = "WHERE entity_id = :eid"
        if start is not None:
            params["start"] = start
            where += " AND time >= :start"
        rows = db.session.execute(text(f"""
            SELECT
                EXTRACT(DOW FROM time)  AS dow,
                EXTRACT(HOUR FROM time) AS hour,
                COUNT(*) AS count
            FROM request_logs
            {where}
            GROUP BY 1, 2
        """), params).all()
    elif start is not None:
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
