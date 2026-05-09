from ..extensions import db


class GroupModelAccess(db.Model):
    """Per-group model access override.

    Mirrors entity_model_access but applies to all members of the group.
    Entity-level overrides take precedence over these rows.
    access_type values: 'whitelist', 'blacklist', 'graylist'.
    """

    __tablename__ = "group_model_access"

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, comment="The group the override applies to")
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, comment="The model being overridden")
    # 'whitelist' | 'blacklist' | 'graylist'
    access_type = db.Column(db.String(20), nullable=False, comment="whitelist (always allowed), blacklist (always denied), or graylist (requires consent)")

    model_config = db.relationship("ModelConfig")

    __table_args__ = (
        db.UniqueConstraint("group_id", "model_config_id", name="uq_gma_group_model"),
        {"comment": "Per-group model access overrides; lower priority than entity_model_access rows"},
    )
