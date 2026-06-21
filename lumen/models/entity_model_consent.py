from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from ..extensions import db


class EntityModelConsent(db.Model):
    """Records that an entity has acknowledged a model that requires consent.

    A row here is required before a model with needs_ack=true can be used.
    Consent is per-entity per-model and is recorded once; it is not revoked
    automatically when the model's acknowledgement message changes.
    """

    __tablename__ = "entity_model_consents"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The consenting entity")
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="The model for which consent was given")
    consented_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp when the entity accepted the model notice")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_emc_entity_model"),
        {"comment": "Records entity acknowledgement of a model that requires consent; a row is required before use"},
    )
