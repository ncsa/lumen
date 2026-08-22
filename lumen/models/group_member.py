from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.timeutils import utcnow

from ..extensions import db
from .entity import Entity


class GroupMember(db.Model):
    """Association between an entity and a group.

    An entity may belong to multiple groups. config_managed=True rows were
    assigned automatically at login by a group's auto-join rules and are
    reconciled (added/removed) at each login.
    """

    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    group_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), comment="The group")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The member entity")
    # When true, this membership was assigned automatically at login by the
    # group's rules; the login reconciler adds and removes these rows.
    config_managed: Mapped[bool] = mapped_column(db.Boolean, default=False, comment="When true, assigned automatically at login by the group's auto-join rules; reconciled at each login")
    is_owner: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False, comment="True for the group owner; at most one owner per group (enforced by app logic)")
    # Null on rows that predate the column — the join time of those members is unknown.
    joined_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp when the membership was created; null for memberships that predate this column")

    entity: Mapped["Entity"] = relationship(backref="group_memberships")

    __table_args__ = (
        db.UniqueConstraint("group_id", "entity_id"),
        db.Index("ix_group_members_entity_id", "entity_id"),
        # At most one owner per group, enforced in the database: two concurrent
        # ownership transfers would otherwise both commit is_owner=True, after
        # which get_group_owner()'s scalar_one_or_none() raises for everyone.
        db.Index(
            "uq_group_members_owner",
            "group_id",
            unique=True,
            postgresql_where=db.text("is_owner"),
            sqlite_where=db.text("is_owner"),
        ),
        {"comment": "Association between entities and groups; an entity may belong to multiple groups"},
    )


def get_group_owner(group_id: int):
    """The user Entity that owns this group, or None if no owner is set."""
    return db.session.execute(
        select(Entity)
        .join(GroupMember, GroupMember.entity_id == Entity.id)
        .where(
            GroupMember.group_id == group_id,
            GroupMember.is_owner == True,  # noqa: E712 — SQL comparison, not a truth check
        )
    ).scalar_one_or_none()


def is_group_owner(user_entity_id: int, group_id: int) -> bool:
    """True if user_entity_id is the owner of group_id."""
    assoc = db.session.execute(
        select(GroupMember).filter_by(
            entity_id=user_entity_id,
            group_id=group_id,
            is_owner=True,
        )
    ).scalar_one_or_none()
    return assoc is not None
