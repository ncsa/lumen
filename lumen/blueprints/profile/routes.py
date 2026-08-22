import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import delete, func, select, text

from lumen.decorators import is_admin as _is_admin
from lumen.decorators import is_admin_eligible, login_required
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.conversation import Conversation
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_manager import get_managed_projects
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.message import Message
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.services.crypto import hash_api_key
from lumen.services.llm import bulk_model_access_info, get_model_access_status, get_pool_limit, model_notices
from lumen.timeutils import utcnow

profile_bp = Blueprint("profile", __name__)


def _gravatar_url(email: str, size: int = 80) -> str:
    h = hashlib.md5((email or "").strip().lower().encode(), usedforsecurity=False).hexdigest()
    return f"https://www.gravatar.com/avatar/{h}?s={size}&d=mp"


def _entity_groups(eid: int) -> list:
    return db.session.execute(
        select(Group).join(GroupMember, Group.id == GroupMember.group_id)
        .where(GroupMember.entity_id == eid)
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
    result = []
    for mc in all_models:
        access_status = access_statuses.get(mc.id, "allowed")
        consented = (mc.id in consent_map) if access_status == "needs_ack" else None
        u = usage_by_model.get(mc.model_name, {})
        model_status = "disabled" if not mc.active else _endpoint_status(eps_by_model.get(mc.id, []))
        notice, early_notice = model_notices(mc) if access_status == "needs_ack" else (None, None)
        result.append({
            "model_name": mc.model_name,
            "model_url": url_for("models_page.detail", model_name=mc.model_name),
            "notice": notice,
            "early_notice": early_notice,
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
    # Lifetime conversations started, not a row count: the counter survives
    # deleting conversations and disabling storage.
    conversation_count = db.session.scalar(
        select(EntityStat.conversations).filter_by(entity_id=eid)
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


def _build_project_list(eid: int) -> list:
    """Active project entities this user manages, with aggregated usage stats.

    Skips non-user entities (e.g. when _get_profile_data is called for a
    project via projects.detail), since projects don't manage other projects.
    """
    entity = db.session.get(Entity, eid)
    if entity is None or entity.entity_type != "user":
        return []
    projects = get_managed_projects(eid)
    if not projects:
        return []
    stat_rows = db.session.execute(
        select(
            EntityStat.entity_id.label("eid"),
            func.coalesce(EntityStat.requests, 0).label("requests"),
            func.coalesce(EntityStat.input_tokens + EntityStat.output_tokens, 0).label("tokens"),
            func.coalesce(EntityStat.cost, 0).label("cost"),
        ).where(EntityStat.entity_id.in_([p.id for p in projects]))
    ).all()
    stats = {r.eid: r for r in stat_rows}
    return [
        {
            "id": p.id,
            "name": p.name,
            "detail_url": url_for("projects.detail", sid=p.id),
            "requests": int(getattr(stats.get(p.id), "requests", 0)),
            "tokens": int(getattr(stats.get(p.id), "tokens", 0)),
            "cost": float(getattr(stats.get(p.id), "cost", 0)),
            "created_at": p.created_at,
        }
        for p in projects
    ]


def _get_profile_data(eid: int) -> dict:
    chat_agg, conversation_count = _fetch_chat_stats(eid)
    api_keys = db.session.execute(select(APIKey).filter_by(entity_id=eid).order_by(APIKey.created_at)).scalars().all()
    # Fetch model context once and build both the usage list and the access list from it.
    all_models, eps_by_model, access_statuses, consent_map = _fetch_model_context(eid)
    model_usage, total_tokens_used, total_cost = _build_model_usage(eid, all_models, eps_by_model, access_statuses)
    usage_by_model = {u["model_name"]: u for u in model_usage}
    model_access_list = _build_model_access_list(usage_by_model, all_models, eps_by_model, access_statuses, consent_map)
    coin_pool = _build_coin_pool(eid)
    project_list = _build_project_list(eid)
    return {
        "chat_agg": chat_agg,
        "conversation_count": conversation_count,
        "api_keys": api_keys,
        "model_usage": model_usage,
        "model_access_list": model_access_list,
        "coin_pool": coin_pool,
        "project_list": project_list,
        "status": {"total_tokens_used": total_tokens_used, "total_cost": total_cost},
    }


@profile_bp.route("/profile")
@login_required
def index():
    entity_id = session["entity_id"]
    data = _get_profile_data(entity_id)

    profile_entity = db.session.get(Entity, entity_id)
    # The user's own limit row (not an inherited group/default pool), used to
    # prefill the admin edit dialog without materializing inherited values.
    user_limit = db.session.execute(
        select(EntityLimit).filter_by(entity_id=entity_id)
    ).scalar_one_or_none()
    return render_template(
        "profile.html", **data,
        profile_entity=profile_entity,
        gravatar_url=_gravatar_url(profile_entity.email if profile_entity else "", size=230),
        profile_groups=_entity_groups(entity_id),
        admin_eligible=is_admin_eligible(profile_entity),
        admin_mode=bool(session.get("admin_mode")),
        user_limit=user_limit,
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


def _purge_conversations(entity_id: int) -> int:
    """Delete all conversations (and their messages) for an entity. Caller commits."""
    db.session.execute(delete(Message).where(
        Message.conversation_id.in_(select(Conversation.id).where(Conversation.entity_id == entity_id))
    ))
    result = db.session.execute(delete(Conversation).where(Conversation.entity_id == entity_id))
    return result.rowcount


@profile_bp.route("/profile/conversations", methods=["DELETE"])
@login_required
def purge_conversations():
    deleted = _purge_conversations(session["entity_id"])
    db.session.commit()
    return jsonify({"deleted": deleted})


@profile_bp.route("/profile/settings/store-conversations", methods=["POST"])
@login_required
def set_store_conversations():
    data = request.get_json() or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "'enabled' must be a boolean"}), HTTPStatus.BAD_REQUEST

    entity = db.session.get(Entity, session["entity_id"])
    entity.store_conversations = enabled
    deleted = 0 if enabled else _purge_conversations(entity.id)
    db.session.commit()
    return jsonify({"store_conversations": enabled, "deleted": deleted})


@profile_bp.route("/profile/settings/admin-mode", methods=["POST"])
@login_required
def set_admin_mode():
    entity = db.session.get(Entity, session["entity_id"])
    if not is_admin_eligible(entity):
        return jsonify({"error": "Forbidden"}), HTTPStatus.FORBIDDEN

    data = request.get_json() or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "'enabled' must be a boolean"}), HTTPStatus.BAD_REQUEST

    session["admin_mode"] = enabled
    # The nav cache stores is_admin; drop it so the header re-renders with the new mode.
    session.pop("_nav", None)
    return jsonify({"admin_mode": enabled})


@profile_bp.route("/profile/consent/<path:model_name>", methods=["POST"])
@login_required
def user_consent(model_name):
    entity_id = session["entity_id"]
    config = db.first_or_404(select(ModelConfig).where(ModelConfig.model_name == model_name, ModelConfig.active))

    if get_model_access_status(entity_id, config.id) != "needs_ack":
        return jsonify({"error": "Model does not require acknowledgement for this user"}), HTTPStatus.BAD_REQUEST

    row = db.session.execute(
        select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=config.id)
    ).scalar_one_or_none()
    if row is None:
        row = EntityModelConsent(entity_id=entity_id, model_config_id=config.id)
        db.session.add(row)
    now = utcnow()
    if config.needs_ack and row.consented_at is None:
        row.consented_at = now
    if config.early_access and row.early_access_at is None:
        row.early_access_at = now
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
    """Start of the window, **floored to the hour**.

    The alignment is load-bearing, not cosmetic. Every aggregate this module
    reads is hourly: ``bucket`` is ``time_bucket('1 hour', time)``, the hour's
    start. On an unaligned start the aggregate arms (``bucket >= :start``) and
    the raw arms (``time >= :start``) are not the same predicate — a request at
    09:45 has ``bucket = 09:00``, so with ``start = 09:30`` the raw arm counts
    it and the aggregate arm drops it, along with everything else in the
    window's first partial hour. Flooring makes the two predicates identical,
    because for an hour-aligned ``S``, ``time_bucket('1 hour', time) >= S`` is
    true exactly when ``time >= S``.

    The cost is that a window can reach up to 59 minutes further back than its
    name suggests. That is invisible at the page's coarsest-to-finest bucket
    widths (1 day / 1 week / 1 month) and is the same widening for every chart
    on the page, per-entity and org-wide alike, which is the point.
    """
    cfg = _USAGE_PERIODS.get(period_str, _USAGE_PERIODS["week"])
    if cfg["offset"] is None:
        return None
    return (datetime.now(timezone.utc) - cfg["offset"]).replace(
        minute=0, second=0, microsecond=0
    )


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


# --- Per-entity usage source -------------------------------------------------
#
# The five per-entity /usage queries below read the
# ``request_counts_hourly_by_entity`` continuous aggregate, with a fallback to
# raw ``request_logs`` for any window the aggregate does not yet cover.
#
# TODO(phase8): delete ``_entity_aggregate_covers``, its helper, and the raw
# arm of each of the five queries once ``flask backfill-aggregate`` is
# confirmed to have run in production — i.e. once the aggregate's earliest
# bucket is at or before the oldest surviving row in ``request_logs``.
#
# Why the fallback is not optional: ``entrypoint.sh`` runs ``flask db upgrade``
# at container start, so the aggregate and its refresh policy exist from the
# moment this code deploys, while the backfill is a separate manual command
# that nothing gates on. Once the policy job runs it advances the view's
# watermark, and real-time aggregation only scans raw rows *above* the
# watermark — so history older than the policy's ``start_offset`` that was
# never materialised is INVISIBLE through the view, not merely stale. Without
# this fallback every user's All Time / Month / Week chart would read
# near-empty from the instant of deploy until a human remembered the CLI.


def _entity_aggregate_earliest_bucket():
    """Earliest *materialized* bucket held by ``request_counts_hourly_by_entity``
    (None if empty).

    Reads the aggregate's materialization hypertable, not the real-time view
    itself: the view is ``materialized_only = false`` and in the un-backfilled
    state it is created ``WITH NO DATA``, so ``MIN(bucket)`` against it scans
    every raw ``request_logs`` row below the policy watermark. Only the
    materialization hypertable holds what has actually been refreshed — which
    is exactly what ``_entity_aggregate_covers`` needs to decide — and it is a
    fast ``MIN`` regardless of backfill lag. Resolved via the catalog — the
    same approach as ``commands.py`` (which, to be precise, also resolves the
    bucket *column*; here that column is hardcoded to ``bucket``, the
    aggregate's first column). Then cached on ``g``. PostgreSQL only; every
    caller sits behind a dialect check.
    """
    if "usage_entity_agg_earliest" not in g:
        mat_table = db.session.execute(text(
            "SELECT materialization_hypertable_schema || '.' || "
            "materialization_hypertable_name "
            "FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = 'request_counts_hourly_by_entity'"
        )).scalar()
        # The catalog row exists whenever the aggregate does (the pair is
        # created atomically), so this None-guard only protects against a
        # dialect the caller should have filtered out. Falling back to the view
        # name keeps an unexpected state from turning into a confusing
        # "FROM None" instead of a normal query error.
        g.usage_entity_agg_earliest = db.session.execute(
            text(f"SELECT MIN(bucket) FROM {mat_table or 'request_counts_hourly_by_entity'}")
        ).scalar()
    return g.usage_entity_agg_earliest


def _entity_aggregate_covers(start):
    """True when the aggregate covers the window starting at ``start``.

    Two ways it can. Either it reaches back past the window's own start, or it
    reaches back past the oldest row that still exists at all — the second is
    what makes "All time" (``start is None``) answerable, and it is also what
    stays true after retention drops raw chunks, since the raw table is then
    the younger of the two. The second query only runs when the first test
    fails, and its result is cached alongside the first.
    """
    earliest = _entity_aggregate_earliest_bucket()
    if earliest is None:
        return False
    if start is not None and earliest <= start:
        return True
    if "usage_raw_earliest" not in g:
        g.usage_raw_earliest = db.session.execute(
            text("SELECT MIN(time) FROM request_logs")
        ).scalar()
    return g.usage_raw_earliest is None or earliest <= g.usage_raw_earliest


# ``request_logs.entity_id`` is ON DELETE SET NULL, so a hard-deleted entity's
# raw rows answer for nobody. The aggregate materialised the id at refresh time
# and never re-evaluates the foreign key, so it keeps that entity's groups for
# as long as the view lives. Both arms carry this clause so they keep answering
# alike: without it the same admin URL returns nothing before the refresh policy
# first runs and a full history afterwards. For an entity that still exists it
# is one primary-key probe, evaluated once.
_ENTITY_STILL_EXISTS = " AND EXISTS (SELECT 1 FROM entities WHERE id = :eid)"


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
        use_agg = _entity_aggregate_covers(start)
        where = "WHERE entity_id = :eid" + _ENTITY_STILL_EXISTS
        if start is not None:
            params["start"] = start
            where += " AND bucket >= :start" if use_agg else " AND time >= :start"
        # The two statements answer the same question over the same window and
        # are kept side by side so that equality stays auditable by reading.
        # The equality holds only because ``_usage_period_start`` floors to the
        # hour: `bucket >= :start` and `time >= :start` are the same predicate
        # on an hour-aligned start and on no other.
        agg_sql = f"""
            SELECT
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COALESCE(SUM(cost), 0.0)
            FROM request_counts_hourly_by_entity
            {where}
        """
        raw_sql = f"""
            SELECT
                COALESCE(COUNT(*), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COALESCE(SUM(cost), 0.0)
            FROM request_logs
            {where}
        """
        row = db.session.execute(text(agg_sql if use_agg else raw_sql), params).one()
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
        use_agg = _entity_aggregate_covers(start)
        where = "WHERE entity_id = :eid" + _ENTITY_STILL_EXISTS
        if start is not None:
            params["start"] = start
            where += " AND bucket >= :start" if use_agg else " AND time >= :start"
        # Side by side so the equality stays auditable; see _entity_aggregate_covers.
        agg_sql = f"""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), bucket) AS period, SUM(requests) AS count
            FROM request_counts_hourly_by_entity
            {where}
            GROUP BY 1 ORDER BY 1
        """
        raw_sql = f"""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), time) AS period, COUNT(*) AS count
            FROM request_logs
            {where}
            GROUP BY 1 ORDER BY 1
        """
        rows = db.session.execute(text(agg_sql if use_agg else raw_sql), params).all()
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
        use_agg = _entity_aggregate_covers(start)
        where = "WHERE entity_id = :eid" + _ENTITY_STILL_EXISTS
        if start is not None:
            params["start"] = start
            where += " AND bucket >= :start" if use_agg else " AND time >= :start"
        # Side by side so the equality stays auditable; see _entity_aggregate_covers.
        agg_sql = f"""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), bucket) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_counts_hourly_by_entity
            {where}
            GROUP BY 1 ORDER BY 1
        """
        raw_sql = f"""
            SELECT time_bucket(CAST(:bucket AS INTERVAL), time) AS period,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM request_logs
            {where}
            GROUP BY 1 ORDER BY 1
        """
        rows = db.session.execute(text(agg_sql if use_agg else raw_sql), params).all()
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
        use_agg = _entity_aggregate_covers(start)
        where = "WHERE rl.entity_id = :eid" + _ENTITY_STILL_EXISTS
        if start is not None:
            params["start"] = start
            where += " AND rl.bucket >= :start" if use_agg else " AND rl.time >= :start"
        # Side by side so the equality stays auditable; see _entity_aggregate_covers.
        # The alias stays `rl` in both arms so the shared WHERE clause fits either.
        agg_sql = f"""
            SELECT mc.model_name, SUM(rl.requests) AS requests
            FROM request_counts_hourly_by_entity rl
            JOIN model_configs mc ON rl.model_config_id = mc.id
            {where}
            GROUP BY mc.model_name ORDER BY requests DESC
        """
        raw_sql = f"""
            SELECT mc.model_name, COUNT(*) AS requests
            FROM request_logs rl
            JOIN model_configs mc ON rl.model_config_id = mc.id
            {where}
            GROUP BY mc.model_name ORDER BY requests DESC
        """
        rows = db.session.execute(text(agg_sql if use_agg else raw_sql), params).all()
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
        use_agg = _entity_aggregate_covers(start)
        where = "WHERE entity_id = :eid" + _ENTITY_STILL_EXISTS
        if start is not None:
            params["start"] = start
            where += " AND bucket >= :start" if use_agg else " AND time >= :start"
        # Side by side so the equality stays auditable; see _entity_aggregate_covers.
        # EXTRACT(HOUR FROM bucket) is why the aggregate buckets hourly: on a
        # daily bucket every row would collapse to hour 0 and the 7x24 grid
        # would silently become a single column.
        agg_sql = f"""
            SELECT
                EXTRACT(DOW FROM bucket)  AS dow,
                EXTRACT(HOUR FROM bucket) AS hour,
                SUM(requests) AS count
            FROM request_counts_hourly_by_entity
            {where}
            GROUP BY 1, 2
        """
        raw_sql = f"""
            SELECT
                EXTRACT(DOW FROM time)  AS dow,
                EXTRACT(HOUR FROM time) AS hour,
                COUNT(*) AS count
            FROM request_logs
            {where}
            GROUP BY 1, 2
        """
        rows = db.session.execute(text(agg_sql if use_agg else raw_sql), params).all()
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
