from datetime import datetime
from ..extensions import db


class ModelStat(db.Model):
    __tablename__ = "model_stats"
    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", "source"),
    )

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False)
    source = db.Column(db.String(8), nullable=False)  # 'chat' or 'api'
    requests = db.Column(db.Integer, default=0, nullable=False)
    input_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    output_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    cost = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    last_used_at = db.Column(db.DateTime, default=datetime.utcnow)
