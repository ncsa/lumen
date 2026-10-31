from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from ..extensions import db


class ModelStat(db.Model):
    """Aggregated usage counters per entity per model per source.

    One row per (entity, model, source) triple, used for the usage dashboard.
    Before the stats cutover billing increments the counters on every proxied
    request; from the cutover on the stats fold adds request_logs into them.
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
    # The part of the totals above that the stats fold added since the cutover,
    # which check-stats compares with request_logs.
    folded_requests: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", comment="Part of requests added by the stats fold since the cutover; check-stats compares it with request_logs")
    folded_input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Part of input_tokens added by the stats fold since the cutover")
    folded_output_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Part of output_tokens added by the stats fold since the cutover")
    folded_audio_seconds: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Part of audio_seconds added by the stats fold since the cutover")
    folded_cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, server_default="0", comment="Part of cost added by the stats fold since the cutover")

    __table_args__ = (
        db.UniqueConstraint("entity_id", "model_config_id", "source"),
        db.Index("ix_model_stats_entity_id", "entity_id"),
        db.Index("ix_model_stats_model_config_id", "model_config_id"),
        {"comment": "Aggregated usage counters per (entity, model, source) triple; folded from request_logs (bumped per request before the stats cutover)"},
    )
