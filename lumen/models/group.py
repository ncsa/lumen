from datetime import datetime, timezone
from ..extensions import db


class Group(db.Model):
    """Named collection of entities used for bulk policy assignment.

    Groups can be managed via the admin UI or driven from config.yaml
    (config_managed=True). Model access policy and coin limits are inherited
    by all group members unless overridden at the entity level.
    """

    __tablename__ = "groups"
    __table_args__ = {"comment": "Named collections of entities for bulk model access and coin limit policy assignment"}

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    name = db.Column(db.String(128), unique=True, nullable=False, comment="Unique group identifier")
    description = db.Column(db.Text, nullable=True, comment="Optional description shown in the admin UI")
    active = db.Column(db.Boolean, default=True, nullable=False, comment="Inactive groups have no effect on member access")
    # When true, membership and settings are controlled by config.yaml
    config_managed = db.Column(db.Boolean, default=False, nullable=False, comment="When true, group and membership are controlled by config.yaml")
    # Default access policy for models not listed in group_model_access
    # 'whitelist' | 'blacklist' | 'graylist'
    model_access_default = db.Column(db.String(20), nullable=True, comment="Default model access policy for this group: whitelist, blacklist, or graylist")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")

    members = db.relationship("GroupMember", backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    limit = db.relationship("GroupLimit", backref="group", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_access = db.relationship("GroupModelAccess", backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
