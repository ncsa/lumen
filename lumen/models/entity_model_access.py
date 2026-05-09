from ..extensions import db


class EntityModelAccess(db.Model):
    """Per-entity model access override.

    Each row pins a specific model to a policy for one entity. Models not listed
    here fall back to entity.model_access_default, then group policy.
    access_type values: 'whitelist' (always allowed), 'blacklist' (always denied),
    'graylist' (allowed only after consent in entity_model_consents).
    Entity-level rows take precedence over group_model_access rows.
    """

    __tablename__ = "entity_model_access"

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The entity the override applies to")
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, comment="The model being overridden")
    # 'whitelist' | 'blacklist' | 'graylist'
    access_type = db.Column(db.String(20), nullable=False, comment="whitelist (always allowed), blacklist (always denied), or graylist (requires consent)")

    model_config = db.relationship("ModelConfig")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_ema_entity_model"),
        {"comment": "Per-entity model access overrides; entity-level takes precedence over group-level"},
    )
