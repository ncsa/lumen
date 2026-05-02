from datetime import datetime, timezone

from ..extensions import db


class EntityModelConsent(db.Model):
    __tablename__ = "entity_model_consents"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False)
    consented_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_emc_entity_model"),
    )
