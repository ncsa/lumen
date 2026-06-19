from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class EntityStat(db.Model):
    """Aggregated usage totals per entity across all models and sources.

    One row per entity. Counters are incremented atomically on every proxied
    request alongside model_stats, enabling O(1) per-entity lookups without
    a GROUP BY scan over model_stats.
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
    last_used_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp of the most recent request by this entity")

    __table_args__ = (
        {"comment": "Aggregated usage totals per entity across all models and sources; updated on every proxied request"},
    )
