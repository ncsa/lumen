import hashlib
from http import HTTPStatus

from flask import Blueprint, abort, jsonify, render_template, request, session, url_for
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from lumen.blueprints.admin.routes import apply_group_coin_pool_edit
from lumen.decorators import admin_required, is_admin, login_required
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import (
    GroupMember,
    get_group_owner,
    is_group_owner,
)
from lumen.models.group_rule import GroupRule
from lumen.models.model_config import ModelConfig
from lumen.models.model_group_access import ModelGroupAccess

groups_bp = Blueprint("groups", __name__)

# Must match the options rendered by the frontend per-page selector.
_VALID_PER_PAGE = {25, 50, 100, 200}


def _require_group_access(entity_id: int, gid: int):
    """Abort 403 if entity_id is not an admin and is not a member of group gid."""
    entity = db.session.get(Entity, entity_id)
    if not is_admin(entity):
        assoc = db.session.execute(
            select(GroupMember).filter_by(entity_id=entity_id, group_id=gid)
        ).scalar_one_or_none()
        if not assoc:
            abort(HTTPStatus.FORBIDDEN)


def _require_group_admin(entity_id: int, gid: int):
    """Abort 403 unless caller is a global admin or the group's owner.

    Owner-level actions: add/remove members, transfer ownership, grant and
    revoke models, edit the profile, toggle. Regular members are rejected here.
    """
    entity = db.session.get(Entity, entity_id)
    if not is_admin(entity) and not is_group_owner(entity_id, gid):
        abort(HTTPStatus.FORBIDDEN)


def _scoped_group_ids(entity_id, entity):
    """Full set of group ids visible to this caller (admins: all; others: member of)."""
    if is_admin(entity):
        return db.session.execute(select(Group.id)).scalars().all()
    return db.session.execute(
        select(GroupMember.group_id).where(GroupMember.entity_id == entity_id)
    ).scalars().all()


def _owned_group_ids(group_ids=None):
    """Ids of groups that have an owner.

    A group with no owner has nobody accountable for its membership, so its
    member list and rolled-up usage are withheld from everyone but admins —
    otherwise any member of a large ownerless group (e.g. one created by a
    config.yaml group rule) could enumerate the whole directory through it.
    """
    stmt = select(GroupMember.group_id).where(
        GroupMember.is_owner == True  # noqa: E712 — SQL comparison, not a truth check
    ).distinct()
    if group_ids is not None:
        stmt = stmt.where(GroupMember.group_id.in_(group_ids))
    return set(db.session.execute(stmt).scalars().all())


def _reject_auto_membership(group):
    """409 for manual membership changes on an auto-join group, or None.

    An auto-join group's membership is entirely rule-driven: the reconciler
    adds and removes members at login, so hand-edits would either be undone at
    the member's next login or linger unremovable. Turn auto-join off first to
    manage members by hand.
    """
    if group.auto_join:
        return jsonify(
            {"error": "Membership of this group is managed by its auto-join rules"}
        ), HTTPStatus.CONFLICT
    return None


_RULE_MATCHES = {"contains", "equals"}


def _parse_rules(raw):
    """Validate a rules payload into GroupRule rows (not yet bound to a group).

    Each entry needs a non-empty field, a match of 'contains' or 'equals', and
    a non-empty value — an empty value with 'contains' would match everyone.
    Returns (rows, error): rows is a list of GroupRule, error a message or None.
    """
    if not isinstance(raw, list):
        return None, "rules must be a list"
    rows = []
    for rule in raw:
        if not isinstance(rule, dict):
            return None, "each rule must be an object"
        field = (rule.get("field") or "").strip()
        match = (rule.get("match") or "").strip()
        value = (rule.get("value") or "").strip()
        if not field:
            return None, "each rule needs a field"
        if match not in _RULE_MATCHES:
            return None, "each rule's match must be 'contains' or 'equals'"
        if not value:
            return None, "each rule needs a value"
        rows.append(GroupRule(field=field, match=match, value=value))
    return rows, None


def _member_count_sq(group_ids=None):
    stmt = select(
        GroupMember.group_id.label("group_id"),
        func.count(GroupMember.id).label("member_count"),
    )
    if group_ids is not None:
        stmt = stmt.where(GroupMember.group_id.in_(group_ids))
    return stmt.group_by(GroupMember.group_id).subquery()


def _model_count_sq(group_ids=None):
    stmt = select(
        ModelGroupAccess.group_id.label("group_id"),
        func.count(ModelGroupAccess.id).label("model_count"),
    )
    if group_ids is not None:
        stmt = stmt.where(ModelGroupAccess.group_id.in_(group_ids))
    return stmt.group_by(ModelGroupAccess.group_id).subquery()


def _usage_sq(group_ids=None):
    """Usage summed over the group's member entities.

    A member in two groups contributes to both totals: these are per-group
    views of member activity, not a partition of overall traffic.

    group_ids narrows the aggregate to the caller's visible groups. Without it
    every request would aggregate the whole membership table just to render one
    page, which is what dominates the query for a user who belongs to a few
    groups. Admins see everything, so they pass None and no filter is added.
    """
    stmt = (
        select(
            GroupMember.group_id.label("group_id"),
            func.coalesce(func.sum(EntityStat.requests), 0).label("requests"),
            func.coalesce(func.sum(EntityStat.input_tokens + EntityStat.output_tokens), 0).label("tokens"),
            func.coalesce(func.sum(EntityStat.cost), 0).label("cost"),
            func.max(EntityStat.last_used_at).label("last_used_at"),
        )
        .join(EntityStat, EntityStat.entity_id == GroupMember.entity_id)
    )
    if group_ids is not None:
        stmt = stmt.where(GroupMember.group_id.in_(group_ids))
    return stmt.group_by(GroupMember.group_id).subquery()


@groups_bp.route("/groups", methods=["GET"])
@login_required
def index():
    entity_id = session["entity_id"]
    entity = db.session.get(Entity, entity_id)

    # Summary cards reflect the full visible set, independent of the paginated table.
    group_ids = _scoped_group_ids(entity_id, entity)
    # Ownerless groups contribute no members or usage for a non-admin (see
    # _owned_group_ids), so they are left out of the totals rather than
    # silently inflating them.
    agg_ids = group_ids if is_admin(entity) else sorted(_owned_group_ids(group_ids))
    if agg_ids:
        total_members = db.session.scalar(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id.in_(agg_ids))
        )
        agg = db.session.execute(
            select(
                func.coalesce(func.sum(EntityStat.requests), 0),
                func.coalesce(func.sum(EntityStat.input_tokens + EntityStat.output_tokens), 0),
            )
            .select_from(GroupMember)
            .join(EntityStat, EntityStat.entity_id == GroupMember.entity_id)
            .where(GroupMember.group_id.in_(agg_ids))
        ).one()
        total_requests, total_tokens = int(agg[0]), int(agg[1])
    else:
        total_members = total_requests = total_tokens = 0

    return render_template(
        "groups.html",
        total_groups=len(group_ids),
        total_members=total_members,
        total_requests=total_requests,
        total_tokens=total_tokens,
    )


@groups_bp.route("/groups/data", methods=["GET"])
@login_required
def data():
    """Paginated group rows for the groups table (mirrors /projects/data)."""
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

    # Resolve visibility first so the aggregates below only scan the caller's
    # groups; an unscoped aggregate over every membership dominates the query.
    visible_ids = None
    # Members and usage are withheld for ownerless groups unless the caller is
    # an admin. Narrowing the aggregates to agg_ids (rather than blanking the
    # numbers afterwards) means the hidden values are never computed at all, so
    # they cannot leak through the sort order either.
    agg_ids = None
    if not admin:
        visible_ids = _scoped_group_ids(entity_id, entity)
        if not visible_ids:
            return jsonify({"groups": [], "total": 0, "page": page, "per_page": per_page})
        owned = _owned_group_ids(visible_ids)
        agg_ids = [gid for gid in visible_ids if gid in owned]

    member_sq = _member_count_sq(agg_ids)
    model_sq = _model_count_sq(visible_ids)
    usage_sq = _usage_sq(agg_ids)

    stmt = (
        select(
            Group,
            member_sq.c.member_count.label("members"),
            func.coalesce(model_sq.c.model_count, 0).label("models"),
            usage_sq.c.requests.label("requests"),
            usage_sq.c.tokens.label("tokens"),
            usage_sq.c.cost.label("cost"),
            usage_sq.c.last_used_at.label("last_used_at"),
            GroupLimit.max_coins.label("max_coins"),
            GroupLimit.refresh_coins.label("refresh_coins"),
        )
        .outerjoin(member_sq, Group.id == member_sq.c.group_id)
        .outerjoin(model_sq, Group.id == model_sq.c.group_id)
        .outerjoin(usage_sq, Group.id == usage_sq.c.group_id)
        .outerjoin(GroupLimit, Group.id == GroupLimit.group_id)
    )

    # Everyone sees inactive groups: admins see all, members see theirs — an
    # owner must be able to reach a deactivated group to re-enable it.
    if visible_ids is not None:
        stmt = stmt.where(Group.id.in_(visible_ids))

    if search:
        stmt = stmt.where(Group.name.ilike(f"%{search}%"))

    sort_col = {
        "name": Group.name,
        "members": member_sq.c.member_count,
        "models": func.coalesce(model_sq.c.model_count, 0),
        "active": Group.active,
        "last_used": usage_sq.c.last_used_at,
        "requests": usage_sq.c.requests,
        "tokens": usage_sq.c.tokens,
        "cost": usage_sq.c.cost,
        "max_coins": GroupLimit.max_coins,
        "refresh_coins": GroupLimit.refresh_coins,
        "created": Group.created_at,
    }.get(sort, Group.name)
    direction = sort_col.desc().nullslast() if order == "desc" else sort_col.asc().nullslast()
    stmt = stmt.order_by(direction)

    # Count from the base Group filters only: the aggregate subqueries are
    # grouped by group_id, so the outer left joins are at most 1:1 and cannot
    # change the row count — re-running them for the count is pure waste.
    count_stmt = select(func.count()).select_from(Group)
    if visible_ids is not None:
        count_stmt = count_stmt.where(Group.id.in_(visible_ids))
    if search:
        count_stmt = count_stmt.where(Group.name.ilike(f"%{search}%"))
    total = db.session.scalar(count_stmt)
    rows = db.session.execute(stmt.offset((page - 1) * per_page).limit(per_page)).all()

    # Per-row data for the inline edit dialog: whether the caller owns the group.
    owner_ids = {
        m.group_id
        for m in db.session.execute(
            select(GroupMember).filter_by(entity_id=entity_id, is_owner=True)
        ).scalars().all()
    }
    page_ids = [g.id for g, *_ in rows]
    has_owner = _owned_group_ids(page_ids) if page_ids else set()

    return jsonify({
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "members": int(members or 0) if (admin or g.id in has_owner) else None,
                "models": int(models),
                "active": g.active,
                "auto_join": g.auto_join,
                "has_owner": g.id in has_owner,
                "last_used": last_used_at.strftime("%Y-%m-%dT%H:%M:%SZ") if last_used_at else None,
                "requests": int(requests or 0) if (admin or g.id in has_owner) else None,
                "tokens": int(tokens or 0) if (admin or g.id in has_owner) else None,
                "cost": float(cost or 0) if (admin or g.id in has_owner) else None,
                "max_coins": float(max_coins) if max_coins is not None else None,
                "refresh_coins": float(refresh_coins) if refresh_coins is not None else None,
                "created": g.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if g.created_at else None,
                "detail_url": url_for("groups.detail", gid=g.id),
                "is_owner": g.id in owner_ids,
            }
            for g, members, models, requests, tokens, cost, last_used_at, max_coins, refresh_coins in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@groups_bp.route("/groups/<int:gid>")
@login_required
def detail(gid):
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_access(entity_id, gid)

    owner = get_group_owner(gid)
    owner_id = owner.id if owner else None
    entity = db.session.get(Entity, entity_id)
    can_manage = is_admin(entity) or owner_id == entity_id

    # An ownerless group exposes neither its member list nor its rolled-up
    # usage to a non-admin (see _owned_group_ids).
    show_members = is_admin(entity) or owner_id is not None

    member_count = total_requests = total_tokens = None
    total_cost = None
    if show_members:
        member_count = db.session.scalar(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id == gid)
        )
        agg = db.session.execute(
            select(
                func.coalesce(func.sum(EntityStat.requests), 0),
                func.coalesce(func.sum(EntityStat.input_tokens + EntityStat.output_tokens), 0),
                func.coalesce(func.sum(EntityStat.cost), 0),
            )
            .select_from(GroupMember)
            .join(EntityStat, EntityStat.entity_id == GroupMember.entity_id)
            .where(GroupMember.group_id == gid)
        ).one()
        total_requests, total_tokens, total_cost = int(agg[0]), int(agg[1]), float(agg[2])

    granted = db.session.execute(
        select(ModelGroupAccess, ModelConfig)
        .join(ModelConfig, ModelConfig.id == ModelGroupAccess.model_config_id)
        .where(ModelGroupAccess.group_id == gid)
        .order_by(ModelConfig.model_name)
    ).all()
    owner_names = {}
    owner_entity_ids = {mc.owner_entity_id for _, mc in granted if mc.owner_entity_id}
    if owner_entity_ids:
        owner_names = {
            e.id: e.name
            for e in db.session.execute(select(Entity).where(Entity.id.in_(owner_entity_ids))).scalars().all()
        }
    granted_models = [
        {
            "id": mc.id,
            "model_name": mc.model_name,
            "owner_name": owner_names.get(mc.owner_entity_id, "—"),
            "url": url_for("models_page.detail", model_name=mc.model_name),
        }
        for _, mc in granted
    ]

    h = hashlib.md5(group.name.strip().lower().encode(), usedforsecurity=False).hexdigest()
    gravatar_url = f"https://www.gravatar.com/avatar/{h}?s=230&d=identicon&f=y"

    group_limit = db.session.execute(
        select(GroupLimit).filter_by(group_id=gid)
    ).scalar_one_or_none()

    return render_template(
        "group_detail.html",
        group=group,
        owner_id=owner_id,
        can_manage=can_manage,
        gravatar_url=gravatar_url,
        group_limit=group_limit,
        member_count=member_count,
        show_members=show_members,
        granted_models=granted_models,
        total_requests=total_requests,
        total_tokens=total_tokens,
        total_cost=total_cost,
        addable_model_count=len(_addable_models(entity_id, gid)),
    )


@groups_bp.route("/groups/<int:gid>", methods=["PATCH"])
@login_required
def update_group(gid):
    """Edit a group's name/description/active (owner or admin) and coin pool (admin only)."""
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)

    data = request.get_json() or {}
    caller = db.session.get(Entity, entity_id)
    if ("max_coins" in data or "refresh_coins" in data) and not is_admin(caller):
        return jsonify({"error": "Only administrators can change coin limits"}), HTTPStatus.FORBIDDEN

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Group name required"}), HTTPStatus.BAD_REQUEST
        duplicate = db.session.execute(
            select(Group).where(Group.name == name, Group.id != gid)
        ).scalar_one_or_none()
        if duplicate:
            return jsonify({"error": "A group with this name already exists"}), HTTPStatus.CONFLICT
        group.name = name

    if "description" in data:
        group.description = (data.get("description") or "").strip() or None

    error = apply_group_coin_pool_edit(gid, data)
    if error:
        return jsonify({"error": error}), HTTPStatus.BAD_REQUEST

    if "active" in data:
        group.active = bool(data["active"])

    db.session.commit()
    return jsonify({"ok": True, "name": group.name, "active": group.active})


@groups_bp.route("/groups/<int:gid>/toggle", methods=["POST"])
@login_required
def toggle_group(gid):
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    group.active = not group.active
    db.session.commit()
    return jsonify({"active": group.active})


@groups_bp.route("/groups", methods=["POST"])
@login_required
def create_group():
    """Create a group. Self-service: any logged-in user may create one.

    A non-admin becomes the owner and may set nothing but the name. An admin
    may additionally name an owner and set the group's coin pool.
    """
    entity_id = session["entity_id"]
    caller = db.session.get(Entity, entity_id)
    admin = is_admin(caller)

    data = request.get_json() or request.form
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), HTTPStatus.BAD_REQUEST

    # Present-and-non-blank, not truthy: JSON 0 is a real attempt to set a
    # coin limit and must be rejected for non-admins, not silently dropped.
    privileged = [
        k for k in ("owner_email", "max_coins", "refresh_coins")
        if data.get(k) not in (None, "")
    ]
    if data.get("auto_join") or data.get("rules"):
        privileged.append("auto_join")
    if privileged and not admin:
        return jsonify(
            {"error": "Only administrators can set an owner, coin limits, or auto-join rules"}
        ), HTTPStatus.FORBIDDEN

    auto_join = bool(data.get("auto_join"))
    rules, error = _parse_rules(data.get("rules") or [])
    if error:
        return jsonify({"error": error}), HTTPStatus.BAD_REQUEST
    if auto_join and not rules:
        return jsonify({"error": "Auto-join requires at least one rule"}), HTTPStatus.BAD_REQUEST
    if auto_join and (data.get("owner_email") or "").strip():
        return jsonify(
            {"error": "An auto-join group cannot have an owner; its membership is rule-driven"}
        ), HTTPStatus.BAD_REQUEST

    duplicate = db.session.execute(select(Group).where(Group.name == name)).scalar_one_or_none()
    if duplicate:
        return jsonify({"error": "A group with this name already exists"}), HTTPStatus.CONFLICT

    owner_user = None
    if admin:
        owner_email = (data.get("owner_email") or "").strip()
        if owner_email:
            owner_user = db.session.execute(
                select(Entity).filter_by(email=owner_email, entity_type="user")
            ).scalar_one_or_none()
            if not owner_user:
                return jsonify({"error": "Owner user not found"}), HTTPStatus.NOT_FOUND
    else:
        owner_user = caller

    group = Group(
        name=name,
        description=(data.get("description") or "").strip() or None,
        active=True,
        auto_join=auto_join,
    )
    db.session.add(group)
    try:
        db.session.flush()
        for rule in rules:
            rule.group_id = group.id
            db.session.add(rule)

        if owner_user:
            db.session.add(GroupMember(group_id=group.id, entity_id=owner_user.id, is_owner=True))

        if admin:
            error = apply_group_coin_pool_edit(group.id, data)
            if error:
                db.session.rollback()
                return jsonify({"error": error}), HTTPStatus.BAD_REQUEST

        db.session.commit()
    except IntegrityError:
        # The duplicate-name SELECT above can lose a race with a concurrent
        # create; the unique constraint on groups.name then fires here.
        db.session.rollback()
        return jsonify({"error": "A group with this name already exists"}), HTTPStatus.CONFLICT
    return jsonify({"id": group.id, "name": group.name}), HTTPStatus.CREATED


@groups_bp.route("/groups/<int:gid>/rules", methods=["PUT"])
@admin_required
def set_group_rules(gid):
    """Replace a group's auto-join rules. Admin only.

    Rules pull members in based on identity-provider claims at login, so they
    are organization policy — group owners manage members, not rules. Turning
    auto_join off keeps the stored rules dormant; turning it on requires at
    least one rule (matching fails closed on an empty set, so an auto group
    without rules would just be misleading UI).
    """
    group = db.get_or_404(Group, gid)
    data = request.get_json() or {}

    auto_join = bool(data.get("auto_join"))
    rules, error = _parse_rules(data.get("rules") or [])
    if error:
        return jsonify({"error": error}), HTTPStatus.BAD_REQUEST
    if auto_join and not rules:
        return jsonify({"error": "Auto-join requires at least one rule"}), HTTPStatus.BAD_REQUEST

    # An auto-join group is fully automatic: membership comes from the rules
    # alone, so it can have neither an owner nor hand-picked members. Both
    # would be stranded — unremovable through the UI, and the reconciler
    # never touches manual rows.
    if auto_join:
        if get_group_owner(gid) is not None:
            return jsonify(
                {"error": "An auto-join group cannot have an owner; its membership is rule-driven"}
            ), HTTPStatus.CONFLICT
        manual = db.session.execute(
            select(GroupMember).filter_by(group_id=gid, config_managed=False).limit(1)
        ).scalar_one_or_none()
        if manual is not None:
            return jsonify(
                {"error": "Remove manually added members before enabling auto-join; membership becomes rule-driven"}
            ), HTTPStatus.CONFLICT

    # Turning auto-join OFF freezes the current roster: auto-assigned rows
    # become manual so the login reconciler (which deletes config_managed
    # memberships whose group is no longer desired) doesn't drain the group
    # one sign-in at a time. This is exactly the flow the membership-locked
    # 409 tells admins to use.
    if group.auto_join and not auto_join:
        db.session.execute(
            sa_update(GroupMember)
            .where(GroupMember.group_id == gid, GroupMember.config_managed == True)  # noqa: E712
            .values(config_managed=False)
        )

    group.rules = rules
    group.auto_join = auto_join
    db.session.commit()
    return jsonify({
        "auto_join": group.auto_join,
        "rules": [{"field": r.field, "match": r.match, "value": r.value} for r in group.rules],
    })


@groups_bp.route("/groups/<int:gid>", methods=["DELETE"])
@login_required
def delete_group(gid):
    """Soft-delete: deactivate. Unlike projects, the owner may do this too."""
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    group.active = False
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

@groups_bp.route("/groups/<int:gid>/members/data")
@login_required
def members_data(gid):
    """Paginated member rows for the Members tab."""
    entity_id = session["entity_id"]
    db.get_or_404(Group, gid)
    _require_group_access(entity_id, gid)
    # The member list of an ownerless group is admin-only: without this the
    # UI restriction would be trivially bypassable by calling the API directly.
    entity = db.session.get(Entity, entity_id)
    if not is_admin(entity) and get_group_owner(gid) is None:
        abort(HTTPStatus.FORBIDDEN)

    page = max(1, request.args.get("page", 1, type=int))
    per_page = request.args.get("per_page", 25, type=int)
    if per_page not in _VALID_PER_PAGE:
        per_page = 25
    sort = request.args.get("sort", "name")
    order = request.args.get("order", "asc")
    search = (request.args.get("search") or "").strip()

    stmt = (
        select(GroupMember, Entity)
        .join(Entity, Entity.id == GroupMember.entity_id)
        .where(GroupMember.group_id == gid)
    )
    if search:
        stmt = stmt.where(db.or_(Entity.name.ilike(f"%{search}%"), Entity.email.ilike(f"%{search}%")))

    sort_col = {
        "name": Entity.name,
        "email": Entity.email,
        "type": Entity.entity_type,
        "owner": GroupMember.is_owner,
        "joined": GroupMember.joined_at,
    }.get(sort, Entity.name)
    direction = sort_col.desc().nullslast() if order == "desc" else sort_col.asc().nullslast()
    stmt = stmt.order_by(direction)

    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.session.execute(stmt.offset((page - 1) * per_page).limit(per_page)).all()

    return jsonify({
        "members": [
            {
                "id": e.id,
                "name": e.name,
                "email": e.email,
                "type": e.entity_type,
                "active": e.active,
                "is_owner": m.is_owner,
                "config_managed": m.config_managed,
                "joined": m.joined_at.strftime("%Y-%m-%dT%H:%M:%SZ") if m.joined_at else None,
            }
            for m, e in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@groups_bp.route("/groups/<int:gid>/members/search")
@login_required
def search_group_members(gid):
    """Users and projects matching q that are not already members."""
    entity_id = session["entity_id"]
    _require_group_admin(entity_id, gid)

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"entities": []})

    db.get_or_404(Group, gid)

    existing_ids = db.session.execute(
        select(GroupMember.entity_id).where(GroupMember.group_id == gid)
    ).scalars().all()

    stmt = (
        select(Entity)
        .where(
            Entity.active == True,  # noqa: E712 — SQL comparison, not a truth check
            db.or_(Entity.email.ilike(f"%{q}%"), Entity.name.ilike(f"%{q}%")),
        )
        .order_by(Entity.name)
        .limit(10)
    )
    if existing_ids:
        stmt = stmt.where(~Entity.id.in_(existing_ids))

    entities = db.session.execute(stmt).scalars().all()
    return jsonify({
        "entities": [
            {"id": e.id, "name": e.name, "email": e.email, "type": e.entity_type}
            for e in entities
        ]
    })


@groups_bp.route("/groups/<int:gid>/members", methods=["POST"])
@login_required
def add_group_member(gid):
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    error = _reject_auto_membership(group)
    if error:
        return error

    data = request.get_json() or {}
    member_id = data.get("entity_id")
    if not member_id:
        return jsonify({"error": "entity_id required"}), HTTPStatus.BAD_REQUEST

    member = db.session.get(Entity, member_id)
    if not member:
        return jsonify({"error": "Entity not found"}), HTTPStatus.NOT_FOUND
    # The typeahead only offers active entities; enforce the same rule here so
    # it cannot be bypassed by POSTing an id directly.
    if not member.active:
        return jsonify({"error": "Entity is deactivated"}), HTTPStatus.BAD_REQUEST

    existing = db.session.execute(
        select(GroupMember).filter_by(entity_id=member.id, group_id=gid)
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "Already a member of this group"}), HTTPStatus.CONFLICT

    db.session.add(GroupMember(group_id=gid, entity_id=member.id))
    try:
        db.session.commit()
    except IntegrityError:
        # The existing-membership SELECT above can lose a race with a
        # concurrent add; UNIQUE(group_id, entity_id) then fires here.
        db.session.rollback()
        return jsonify({"error": "Already a member of this group"}), HTTPStatus.CONFLICT

    return jsonify({
        "entity_id": member.id, "name": member.name, "email": member.email, "type": member.entity_type,
    }), HTTPStatus.CREATED


@groups_bp.route("/groups/<int:gid>/members/<int:eid>", methods=["DELETE"])
@login_required
def remove_group_member(gid, eid):
    """Remove a member.

    config_managed memberships (stamped by OAuth auto-assignment) are removable
    too — an owner must be able to curate their own group. If the group rule
    still matches, the membership is re-added at that user's next login.
    """
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    error = _reject_auto_membership(group)
    if error:
        return error

    target = db.session.execute(
        select(GroupMember).filter_by(entity_id=eid, group_id=gid)
    ).scalar_one_or_none()
    if not target:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    if target.is_owner:
        return jsonify({"error": "Transfer ownership before removing the owner"}), HTTPStatus.CONFLICT

    db.session.delete(target)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT


@groups_bp.route("/groups/<int:gid>/owner/search")
@login_required
def search_owner_candidates(gid):
    """User members of the group who could become its owner.

    The Change Owner dialog searches here: only existing members qualify
    (ownership is a promotion, not an invitation), only users can own a
    group, and the current owner is excluded.
    """
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    error = _reject_auto_membership(group)
    if error:
        return error

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"entities": []})

    entities = db.session.execute(
        select(Entity)
        .join(GroupMember, GroupMember.entity_id == Entity.id)
        .where(
            GroupMember.group_id == gid,
            GroupMember.is_owner == False,  # noqa: E712 — SQL comparison, not a truth check
            Entity.entity_type == "user",
            Entity.active == True,  # noqa: E712 — SQL comparison, not a truth check
            db.or_(Entity.email.ilike(f"%{q}%"), Entity.name.ilike(f"%{q}%")),
        )
        .order_by(Entity.name)
        .limit(10)
    ).scalars().all()
    return jsonify({
        "entities": [
            {"id": e.id, "name": e.name, "email": e.email, "type": e.entity_type}
            for e in entities
        ]
    })


@groups_bp.route("/groups/<int:gid>/owner", methods=["POST"])
@login_required
def transfer_ownership(gid):
    entity_id = session["entity_id"]
    group = db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    error = _reject_auto_membership(group)
    if error:
        return error

    data = request.get_json() or {}
    new_owner_id = data.get("entity_id")
    if not new_owner_id:
        return jsonify({"error": "entity_id required"}), HTTPStatus.BAD_REQUEST

    new_owner = db.session.get(Entity, new_owner_id)
    if not new_owner:
        return jsonify({"error": "User not found"}), HTTPStatus.NOT_FOUND
    if new_owner.entity_type != "user":
        return jsonify({"error": "Only a user can own a group"}), HTTPStatus.BAD_REQUEST
    if not new_owner.active:
        return jsonify({"error": "User is deactivated"}), HTTPStatus.BAD_REQUEST

    old_owner_assoc = db.session.execute(
        select(GroupMember).filter_by(group_id=gid, is_owner=True)
    ).scalar_one_or_none()

    new_assoc = db.session.execute(
        select(GroupMember).filter_by(entity_id=new_owner_id, group_id=gid)
    ).scalar_one_or_none()
    if new_assoc is None:
        return jsonify(
            {"error": "The new owner must already be a member of this group"}
        ), HTTPStatus.BAD_REQUEST
    if new_assoc is old_owner_assoc:
        return jsonify({"error": "User is already the owner"}), HTTPStatus.CONFLICT

    if old_owner_assoc:
        # Demote and flush BEFORE promoting: SQLAlchemy flushes UPDATEs in
        # primary-key order, so promoting a lower-id row first would
        # transiently put two owners under the non-deferrable unique index
        # and fail every transfer to an older member.
        old_owner_assoc.is_owner = False
        db.session.flush()
    new_assoc.is_owner = True
    # Ownership is a deliberate act: without this, a membership stamped
    # config_managed by OAuth auto-assignment would be deleted by login
    # reconciliation when the rule stops matching — silently removing the
    # group's owner.
    new_assoc.config_managed = False

    try:
        db.session.commit()
    except IntegrityError:
        # uq_group_members_owner: a concurrent transfer committed first.
        db.session.rollback()
        return jsonify({"error": "Ownership changed concurrently; reload and try again"}), HTTPStatus.CONFLICT
    return jsonify({"owner_id": new_owner_id}), HTTPStatus.OK


# ---------------------------------------------------------------------------
# Model grants
# ---------------------------------------------------------------------------

def _addable_models(entity_id, gid):
    """Models the caller may grant to this group that are not already granted.

    Admins may grant any owned model; everyone else only models they own.
    Models with no owner are public already, so granting them is meaningless.
    """
    entity = db.session.get(Entity, entity_id)
    granted_ids = db.session.execute(
        select(ModelGroupAccess.model_config_id).where(ModelGroupAccess.group_id == gid)
    ).scalars().all()

    stmt = select(ModelConfig).where(ModelConfig.owner_entity_id != None)  # noqa: E711
    if not is_admin(entity):
        stmt = stmt.where(ModelConfig.owner_entity_id == entity_id)
    if granted_ids:
        stmt = stmt.where(~ModelConfig.id.in_(granted_ids))
    return db.session.execute(stmt.order_by(ModelConfig.model_name)).scalars().all()


@groups_bp.route("/groups/<int:gid>/models")
@login_required
def group_models(gid):
    entity_id = session["entity_id"]
    db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)
    return jsonify({
        "models": [
            {"id": mc.id, "model_name": mc.model_name}
            for mc in _addable_models(entity_id, gid)
        ]
    })


@groups_bp.route("/groups/<int:gid>/models", methods=["POST"])
@login_required
def add_group_model(gid):
    entity_id = session["entity_id"]
    db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)

    data = request.get_json() or {}
    mid = data.get("model_config_id")
    if not mid:
        return jsonify({"error": "model_config_id required"}), HTTPStatus.BAD_REQUEST

    config = db.session.get(ModelConfig, mid)
    if not config:
        return jsonify({"error": "Model not found"}), HTTPStatus.NOT_FOUND

    caller = db.session.get(Entity, entity_id)
    if not is_admin(caller) and config.owner_entity_id != entity_id:
        return jsonify({"error": "You can only grant models you own"}), HTTPStatus.FORBIDDEN
    if config.owner_entity_id is None:
        return jsonify({"error": "This model has no owner and is already available to everyone"}), HTTPStatus.BAD_REQUEST

    existing = db.session.execute(
        select(ModelGroupAccess).filter_by(model_config_id=mid, group_id=gid)
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "Model already granted to this group"}), HTTPStatus.CONFLICT

    db.session.add(ModelGroupAccess(model_config_id=mid, group_id=gid))
    db.session.commit()
    return jsonify({"id": config.id, "model_name": config.model_name}), HTTPStatus.CREATED


@groups_bp.route("/groups/<int:gid>/models/<int:mid>", methods=["DELETE"])
@login_required
def remove_group_model(gid, mid):
    entity_id = session["entity_id"]
    db.get_or_404(Group, gid)
    _require_group_admin(entity_id, gid)

    grant = db.session.execute(
        select(ModelGroupAccess).filter_by(model_config_id=mid, group_id=gid)
    ).scalar_one_or_none()
    if not grant:
        return jsonify({"error": "Not found"}), HTTPStatus.NOT_FOUND

    db.session.delete(grant)
    db.session.commit()
    return "", HTTPStatus.NO_CONTENT
