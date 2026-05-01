from datetime import datetime
from ..extensions import db


class EntityBalance(db.Model):
    __tablename__ = "entity_balances"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, unique=True)
    coins_left = db.Column(db.Numeric(12, 6), default=0)
    last_refill_at = db.Column(db.DateTime, default=datetime.utcnow)
