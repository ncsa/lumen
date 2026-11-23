"""OpenAI-compatible, read-only admin API under /v1/organization.

Only admin keys (``key_type == 'admin'``) may call it. Ids are prefixed public
uuids (``user-<uuid>``, ``group-<uuid>``); keys, usage, tokens and cost never
appear. Nothing here is billed or written to request_logs.
"""

import hmac
from calendar import timegm
from datetime import datetime
from functools import wraps
from http import HTTPStatus

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import and_, exists, func, or_, select

from lumen.blueprints.api.routes import _api_key_id, _api_limit, _err
from lumen.blueprints.profile.routes import _memberships_by_entity
from lumen.decorators import is_admin_eligible
from lumen.extensions import db, limiter
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_NORMAL, APIKey
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.lookups import (
    GROUP_ID_PREFIX,
    USER_ID_PREFIX,
    parse_prefixed_id,
    prefixed_id,
    user_by_prefixed_id,
)
from lumen.services.api_auth import KeyRejected, resolve_api_key
from lumen.services.llm import REFILL_INTERVAL, _load_pool_state
from lumen.timeutils import utcnow

admin_api_bp = Blueprint("admin_api", __name__, url_prefix="/v1/organization")

# One per-key budget for every admin API route, separate from the proxy's.
admin_api_limit = limiter.shared_limit(_api_limit, scope="admin_api", key_func=_api_key_id)

ADMIN_KEY_REQUIRED = "This endpoint requires an admin key"

LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 100
SORT_BY = ("created_at", "last_active", "email")
SORT_ORDER = ("asc", "desc")
STATUS = {"active": True, "inactive": False}
# Stand-ins for nulls in the sort keys, so keyset comparisons never meet a null.
# A never-active user gets the end of the timeline that sorts last in the
# requested direction; a user without created_at sorts as the oldest.
_EARLIEST = datetime(1970, 1, 1)
_LATEST = datetime(9999, 12, 31)


def admin_key_required(f):
    """Let only an admin key through; set ``g.api_key`` and ``g.entity`` to it and its owner.

    Missing or malformed header: 400; non-ASCII token: 401. The config monitor
    token is compared before ``resolve_api_key`` (which would answer it 401)
    and gets 403, like normal and monitoring keys. Anything ``resolve_api_key``
    rejects keeps its own status.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _err("Missing or malformed Authorization header", status=HTTPStatus.BAD_REQUEST)
        token = auth_header[7:].strip()
        if not token:
            return _err("Empty token", status=HTTPStatus.BAD_REQUEST)
        # Lumen keys are ASCII; anything else cannot match one.
        if not token.isascii():
            return _err("Invalid or revoked API key", "authentication_error", HTTPStatus.UNAUTHORIZED)

        monitor_token = current_app.config.get("YAML_DATA", {}).get("api", {}).get("monitoring", {}).get("token", "")
        # Compare bytes: compare_digest raises TypeError on a non-ASCII str.
        if monitor_token and hmac.compare_digest(token.encode(), str(monitor_token).encode()):
            return _err(ADMIN_KEY_REQUIRED, "permission_error", HTTPStatus.FORBIDDEN)

        resolved = resolve_api_key(token)
        if isinstance(resolved, KeyRejected):
            return _err(resolved.message, "authentication_error", resolved.status)
        api_key, entity = resolved
        if api_key.key_type != KEY_TYPE_ADMIN:
            return _err(ADMIN_KEY_REQUIRED, "permission_error", HTTPStatus.FORBIDDEN)

        g.api_key = api_key
        g.entity = entity
        return f(*args, **kwargs)

    return decorated


def _unix(dt: datetime | None) -> int | None:
    """Unix seconds for a naive-UTC timestamp, as OpenAI returns them."""
    return timegm(dt.utctimetuple()) if dt else None


def _user_object(entity: Entity, groups: list[dict], last_active_at: datetime | None) -> dict:
    """The ``organization.user`` fields of a user."""
    return {
        "object": "organization.user",
        "id": prefixed_id(USER_ID_PREFIX, entity.uuid),
        "name": entity.name,
        "email": entity.email,
        "role": "owner" if is_admin_eligible(entity) else "reader",
        "added_at": _unix(entity.created_at),
        "lumen": {
            "status": "active" if entity.active else "inactive",
            "groups": [{"id": f"{GROUP_ID_PREFIX}{grp['id']}", "name": grp["name"]} for grp in groups],
            "last_active_at": _unix(last_active_at),
        },
    }


def _coins(entity_id: int) -> dict | None:
    """The user's coin pool, None when blocked; allocation -2 means unlimited."""
    pool, balance = _load_pool_state(entity_id)
    if pool is None:
        return None
    max_coins, refresh_coins, starting_coins = pool
    last_refill_at = balance.last_refill_at if balance else None
    # The refiller skips unlimited pools, which can keep a stale refresh rate.
    refills = max_coins != -2 and refresh_coins > 0 and last_refill_at is not None
    next_reset = last_refill_at + REFILL_INTERVAL if refills else None
    return {
        "balance": balance.coins_left if balance else starting_coins,
        "allocation": max_coins,
        "per_hour": refresh_coins,
        "next_reset_at": _unix(next_reset),
    }


def _api_key_count(entity_id: int) -> int:
    """The user's own usable normal keys: admin, monitoring, revoked and expired keys are left out."""
    return db.session.scalar(
        select(func.count()).select_from(APIKey).where(
            APIKey.entity_id == entity_id,
            APIKey.key_type == KEY_TYPE_NORMAL,
            APIKey.revoked_at.is_(None),
            or_(APIKey.expires_at.is_(None), APIKey.expires_at > utcnow()),
        )
    )


@admin_api_bp.route("/users/<user_id>", methods=["GET"])
@admin_key_required
@admin_api_limit
def get_user(user_id):
    entity = user_by_prefixed_id(user_id)
    if entity is None:
        return _err(f"No user found with id '{user_id}'", status=HTTPStatus.NOT_FOUND, err_code="user_not_found")
    last_active_at = db.session.scalar(select(EntityStat.last_used_at).where(EntityStat.entity_id == entity.id))
    user = _user_object(entity, _memberships_by_entity([entity.id]).get(entity.id, []), last_active_at)
    user["lumen"]["coins"] = _coins(entity.id)
    user["lumen"]["api_key_count"] = _api_key_count(entity.id)
    return jsonify(user)


def _param_err(message: str, param: str):
    """A 400 ``invalid_request_error`` naming the offending query parameter."""
    return jsonify({"error": {"message": message, "type": "invalid_request_error", "param": param}}), HTTPStatus.BAD_REQUEST


def _sort_key(sort_by: str, sort_order: str):
    """The non-null sort expression for ``sort_by``; never-active users last in either order."""
    if sort_by == "last_active":
        return func.coalesce(EntityStat.last_used_at, _EARLIEST if sort_order == "desc" else _LATEST)
    if sort_by == "email":
        return func.coalesce(Entity.email, "")
    return func.coalesce(Entity.created_at, _EARLIEST)


@admin_api_bp.route("/users", methods=["GET"])
@admin_key_required
@admin_api_limit
def list_users():
    """Users, keyset-paged on ``(sort value, entity id)``; ``after`` is the last user id of the previous page."""
    args = request.args
    try:
        limit = int(args.get("limit", LIST_LIMIT_DEFAULT))
    except ValueError:
        limit = 0
    if not 1 <= limit <= LIST_LIMIT_MAX:
        return _param_err(f"limit must be an integer from 1 to {LIST_LIMIT_MAX}", "limit")
    sort_by = args.get("sort_by", "created_at")
    if sort_by not in SORT_BY:
        return _param_err(f"sort_by must be one of: {', '.join(SORT_BY)}", "sort_by")
    sort_order = args.get("sort_order", "desc")
    if sort_order not in SORT_ORDER:
        return _param_err("sort_order must be asc or desc", "sort_order")

    filters = [Entity.entity_type == "user"]
    search = args.get("search", "").strip()
    # PostgreSQL text cannot hold NUL: the bind would raise and answer 500.
    if "\x00" in search:
        return _param_err("search must not contain a NUL character", "search")
    if search:
        like = f"%{search}%"
        filters.append(or_(Entity.name.ilike(like), Entity.email.ilike(like)))
    if "group" in args:
        group_uuid = parse_prefixed_id(args["group"], GROUP_ID_PREFIX)
        group_pk = group_uuid and db.session.scalar(select(Group.id).where(Group.uuid == group_uuid))
        if not group_pk:
            return _param_err(f"No group found with id '{args['group']}'; use a group-<uuid> id", "group")
        filters.append(exists().where(GroupMember.group_id == group_pk, GroupMember.entity_id == Entity.id))
    if "status" in args:
        if args["status"] not in STATUS:
            return _param_err("status must be active or inactive", "status")
        filters.append(Entity.active.is_(STATUS[args["status"]]))

    total = db.session.scalar(select(func.count()).select_from(Entity).where(*filters))

    sort_key = _sort_key(sort_by, sort_order)
    page = select(Entity, EntityStat.last_used_at).outerjoin(EntityStat, Entity.id == EntityStat.entity_id).where(*filters)
    if "after" in args:
        after_uuid = parse_prefixed_id(args["after"], USER_ID_PREFIX)
        cursor = after_uuid and db.session.execute(
            select(sort_key, Entity.id)
            .outerjoin(EntityStat, Entity.id == EntityStat.entity_id)
            .where(Entity.uuid == after_uuid, Entity.entity_type == "user")
        ).one_or_none()
        if not cursor:
            return _param_err(f"No user found with id '{args['after']}'", "after")
        after_value, after_pk = cursor
        if sort_order == "desc":
            page = page.where(or_(sort_key < after_value, and_(sort_key == after_value, Entity.id < after_pk)))
        else:
            page = page.where(or_(sort_key > after_value, and_(sort_key == after_value, Entity.id > after_pk)))
    if sort_order == "desc":
        page = page.order_by(sort_key.desc(), Entity.id.desc())
    else:
        page = page.order_by(sort_key.asc(), Entity.id.asc())

    rows = db.session.execute(page.limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    memberships = _memberships_by_entity([entity.id for entity, _ in rows])
    data = [_user_object(entity, memberships.get(entity.id, []), last_active_at) for entity, last_active_at in rows]
    return jsonify({
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": has_more,
        "total": total,
    })
