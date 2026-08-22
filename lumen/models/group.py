from datetime import datetime
from typing import Optional

from sqlalchemy.orm import DynamicMapped, Mapped, mapped_column, relationship

from lumen.timeutils import utcnow

from ..extensions import db


class Group(db.Model):
    """Named collection of entities used for bulk policy assignment.

    Groups are managed entirely in the database. Coin limits are inherited by
    all group members; groups can also be granted access to owned models
    (model_group_access). Groups with auto_join assign membership at OAuth
    login to users matching all of their group_rules.
    """

    __tablename__ = "groups"
    __table_args__ = {"comment": "Named collections of entities for coin limit policy assignment and owned-model grants"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    name: Mapped[str] = mapped_column(db.String(128), unique=True, comment="Unique group identifier")
    description: Mapped[Optional[str]] = mapped_column(db.Text, comment="Optional description shown in the admin UI")
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Inactive groups have no effect on member access")
    # When true, users matching all of this group's rules are added at login
    auto_join: Mapped[bool] = mapped_column(db.Boolean, default=False, comment="When true, users matching all of the group's rules are added as members at OAuth login")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")

    members: DynamicMapped["GroupMember"] = relationship(backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    limit: Mapped[Optional["GroupLimit"]] = relationship(backref="group", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_grants: DynamicMapped["ModelGroupAccess"] = relationship(backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    rules: Mapped[list["GroupRule"]] = relationship(backref="group", order_by="GroupRule.id", cascade="all, delete-orphan", passive_deletes=True)
