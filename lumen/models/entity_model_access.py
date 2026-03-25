from ..extensions import db


class EntityModelAccess(db.Model):
    __tablename__ = "entity_model_access"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False)
    allowed = db.Column(db.Boolean, nullable=False)

    model_config = db.relationship("ModelConfig")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_ema_entity_model"),
    )
