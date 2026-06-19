from datetime import datetime
from typing import Optional

from sqlalchemy.orm import DynamicMapped, Mapped, mapped_column, relationship

from lumen.timeutils import utcnow
from ..extensions import db


class Group(db.Model):
    """Named collection of entities used for bulk policy assignment.

    Groups can be managed via the admin UI or driven from config.yaml
    (config_managed=True). Model access policy and coin limits are inherited
    by all group members unless overridden at the entity level.
    """

    __tablename__ = "groups"
    __table_args__ = {"comment": "Named collections of entities for bulk model access and coin limit policy assignment"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    name: Mapped[str] = mapped_column(db.String(128), unique=True, comment="Unique group identifier")
    description: Mapped[Optional[str]] = mapped_column(db.Text, comment="Optional description shown in the admin UI")
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Inactive groups have no effect on member access")
    # When true, membership and settings are controlled by config.yaml
    config_managed: Mapped[bool] = mapped_column(db.Boolean, default=False, comment="When true, group and membership are controlled by config.yaml")
    # Default access policy for models not listed in group_model_access
    # 'whitelist' | 'blacklist' | 'graylist'
    model_access_default: Mapped[Optional[str]] = mapped_column(db.String(20), comment="Default model access policy for this group: whitelist, blacklist, or graylist")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")

    members: DynamicMapped["GroupMember"] = relationship(backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    limit: Mapped[Optional["GroupLimit"]] = relationship(backref="group", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_access: DynamicMapped["GroupModelAccess"] = relationship(backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
