from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class EntityStat(db.Model):
    """Aggregated usage totals per entity across all models and sources.

    One row per entity, enabling O(1) per-entity lookups without a GROUP BY
    scan over model_stats. Before the stats cutover billing increments the
    counters on every proxied request alongside model_stats; from the cutover
    on the stats fold adds request_logs into them. The conversation counter
    stays synchronous either way.
    """

    __tablename__ = "entity_stats"

    entity_id: Mapped[int] = mapped_column(
        db.Integer,
        db.ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The entity these counters belong to",
    )
    requests: Mapped[int] = mapped_column(db.Integer, default=0, comment="Total request count across all models and sources")
    input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Total input tokens consumed across all models and sources")
    output_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Total output tokens produced across all models and sources")
    audio_seconds: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Total seconds of audio transcribed/translated across all models and sources")
    cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Total cost in USD across all models and sources")
    conversations: Mapped[int] = mapped_column(db.Integer, default=0, comment="Total webchat conversations started; retained even when conversations are deleted or storage is disabled")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp of the most recent request by this entity")
    # The part of the totals above that the stats fold added since the cutover,
    # which check-stats compares with request_logs.
    folded_requests: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", comment="Part of requests added by the stats fold since the cutover; check-stats compares it with request_logs")
    folded_input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Part of input_tokens added by the stats fold since the cutover")
    folded_output_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Part of output_tokens added by the stats fold since the cutover")
    folded_audio_seconds: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Part of audio_seconds added by the stats fold since the cutover")
    folded_cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, server_default="0", comment="Part of cost added by the stats fold since the cutover")

    __table_args__ = (
        {"comment": "Aggregated usage totals per entity across all models and sources; folded from request_logs (bumped per request before the stats cutover)"},
    )
