from datetime import datetime
from ..extensions import db


class APIKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(128), nullable=False, default="")
    key_hash = db.Column(db.String(64), unique=True, nullable=True)
    key_hint = db.Column(db.String(32), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    requests = db.Column(db.Integer, default=0, nullable=False)
    input_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    output_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    cost = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
