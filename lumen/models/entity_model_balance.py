from datetime import datetime
from ..extensions import db


class EntityModelBalance(db.Model):
    __tablename__ = "entity_model_balances"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False)
    tokens_left = db.Column(db.BigInteger, default=0)
    last_refill_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", name="uq_emb_entity_model"),
    )
