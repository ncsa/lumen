from datetime import datetime, timezone
from ..extensions import db


class ModelStat(db.Model):
    """Aggregated usage counters per entity per model per source.

    One row per (entity, model, source) triple. Counters are incremented
    atomically on every proxied request and are used for the usage dashboard.
    For raw per-request data, see request_logs.
    """

    __tablename__ = "model_stats"

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The entity that made the requests")
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, comment="The model used")
    # 'chat' (web UI) or 'api' (API key)
    source = db.Column(db.String(8), nullable=False, comment="Origin of the requests: chat (web UI) or api (API key)")
    requests = db.Column(db.Integer, default=0, nullable=False, comment="Total request count")
    input_tokens = db.Column(db.BigInteger, default=0, nullable=False, comment="Total input tokens consumed")
    output_tokens = db.Column(db.BigInteger, default=0, nullable=False, comment="Total output tokens produced")
    cost = db.Column(db.Numeric(12, 6), default=0, nullable=False, comment="Total cost in USD")
    last_used_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC timestamp of the most recent counted request")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", "source"),
        {"comment": "Aggregated usage counters per (entity, model, source) triple; updated on every proxied request"},
    )
