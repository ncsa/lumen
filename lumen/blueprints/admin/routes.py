from flask import Blueprint, render_template, request, redirect, url_for, abort

from lumen.decorators import admin_required
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.entity_model_balance import EntityModelBalance
from lumen.models.group_model_limit import GroupModelLimit
from lumen.models.model_config import ModelConfig
from lumen.models.entity_model_limit import EntityModelLimit
from lumen.services.llm import get_effective_limit

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@admin_bp.route("/groups")
@admin_required
def groups():
    all_groups = Group.query.order_by(Group.name).all()
    member_counts = {g.id: g.members.count() for g in all_groups}
    return render_template("admin/groups.html", groups=all_groups, member_counts=member_counts)


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
    return redirect(url_for("admin.groups"))


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
    # If referred from user limits page, go back there; otherwise group detail
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
# Users
# ---------------------------------------------------------------------------

@admin_bp.route("/users")
@admin_required
def users():
    all_users = Entity.query.filter_by(entity_type="user").order_by(Entity.name).all()
    # Build group membership map
    user_groups = {}
    for user in all_users:
        memberships = GroupMember.query.filter_by(entity_id=user.id).all()
        groups_list = [Group.query.get(m.group_id) for m in memberships]
        user_groups[user.id] = [g for g in groups_list if g]
    return render_template("admin/users.html", users=all_users, user_groups=user_groups)


@admin_bp.route("/users/<int:eid>/limits")
@admin_required
def user_limits(eid):
    entity = Entity.query.get_or_404(eid)
    limits = EntityModelLimit.query.filter_by(entity_id=eid).all()
    models = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()

    # Compute effective limits per model and current balance
    effective = {}
    balance_rows = {b.model_config_id: b.tokens_left for b in EntityModelBalance.query.filter_by(entity_id=eid).all()}
    tokens_left = {}
    for model in models:
        eff = get_effective_limit(eid, model.id)
        effective[model.id] = eff
        if eff is not None and eff[0] != -2:
            tokens_left[model.id] = balance_rows.get(model.id, eff[2])

    # Build (membership, group, group_limits) tuples
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
