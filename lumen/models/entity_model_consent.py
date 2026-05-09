from datetime import datetime, timezone

from ..extensions import db


class EntityModelConsent(db.Model):
    """Records that an entity has accepted the notice for a graylisted model.

    A row here is required before a model with access_type='graylist' can be
    used. Consent is per-entity per-model and is recorded once; it is not
    revoked automatically when the model notice changes.
    """

    __tablename__ = "entity_model_consents"

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The consenting entity")
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, comment="The model for which consent was given")
    consented_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC timestamp when the entity accepted the model notice")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_emc_entity_model"),
        {"comment": "Records entity acceptance of a graylisted model notice; a row is required before use"},
    )
