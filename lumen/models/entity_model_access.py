from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db


class EntityModelAccess(db.Model):
    """Per-entity model access override.

    Each row pins a specific model to a policy for one entity. Models not listed
    here fall back to entity.model_access_default, then group policy.
    access_type values: 'allowed' (always allowed), 'blocked' (always denied).
    A model's acknowledgement requirement (needs_ack) lives on the model itself.
    Entity-level rows take precedence over group_model_access rows.
    """

    __tablename__ = "entity_model_access"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The entity the override applies to")
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="The model being overridden")
    # 'allowed' | 'blocked'
    access_type: Mapped[str] = mapped_column(db.String(20), comment="'allowed' or 'blocked' for this entity; acknowledgement requirement lives on the model")

    model_config: Mapped["ModelConfig"] = relationship()

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_ema_entity_model"),
        db.Index("ix_entity_model_access_entity_id", "entity_id"),
        {"comment": "Per-entity model access overrides; entity-level takes precedence over group-level"},
    )
