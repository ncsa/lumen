from datetime import datetime
from ..extensions import db


class ModelLimit(db.Model):
    __tablename__ = "model_limits"
    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id"), nullable=False)
    # -1 = no access; -2 = unlimited; positive = token budget
    token_limit = db.Column(db.BigInteger, default=0, nullable=False)
    tokens_per_hour = db.Column(db.Integer, default=0, nullable=False)
    tokens_left = db.Column(db.BigInteger, default=0, nullable=False)
    last_refill_at = db.Column(db.DateTime, default=datetime.utcnow)
