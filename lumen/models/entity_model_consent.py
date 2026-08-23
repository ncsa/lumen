from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class EntityModelConsent(db.Model):
    """Records that an entity has acknowledged a model that requires consent.

    A model can carry two acknowledgement requirements: needs_ack (tracked in
    consented_at) and early_access (tracked in early_access_at). Consent is
    per-entity per-model; a requirement is satisfied when its timestamp is set.
    If a model gains a requirement after the entity consented, the new
    requirement's timestamp is NULL and the entity must acknowledge again.
    """

    __tablename__ = "entity_model_consents"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The consenting entity")
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="The model for which consent was given")
    consented_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp when the entity acknowledged the model notice (needs_ack); NULL if never required")
    early_access_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp when the entity acknowledged the early-access warning; NULL if never required")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_emc_entity_model"),
        {"comment": "Records entity acknowledgement of a model that requires consent; a row is required before use"},
    )
