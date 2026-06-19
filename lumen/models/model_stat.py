from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow
from ..extensions import db


class ModelStat(db.Model):
    """Aggregated usage counters per entity per model per source.

    One row per (entity, model, source) triple. Counters are incremented
    atomically on every proxied request and are used for the usage dashboard.
    For raw per-request data, see request_logs.
    """

    __tablename__ = "model_stats"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The entity that made the requests")
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="The model used")
    # 'chat' (web UI) or 'api' (API key)
    source: Mapped[str] = mapped_column(db.String(8), comment="Origin of the requests: chat (web UI) or api (API key)")
    requests: Mapped[int] = mapped_column(db.Integer, default=0, comment="Total request count")
    input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Total input tokens consumed")
    output_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Total output tokens produced")
    audio_seconds: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Total seconds of audio transcribed/translated")
    cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Total cost in USD")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp of the most recent counted request")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", "source"),
        db.Index("ix_model_stats_entity_id", "entity_id"),
        db.Index("ix_model_stats_model_config_id", "model_config_id"),
        {"comment": "Aggregated usage counters per (entity, model, source) triple; updated on every proxied request"},
    )
