from ..extensions import db


class EntityStat(db.Model):
    """Aggregated usage totals per entity across all models and sources.

    One row per entity. Counters are incremented atomically on every proxied
    request alongside model_stats, enabling O(1) per-entity lookups without
    a GROUP BY scan over model_stats.
    """

    __tablename__ = "entity_stats"

    entity_id = db.Column(
        db.Integer,
        db.ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The entity these counters belong to",
    )
    requests = db.Column(db.Integer, default=0, nullable=False, comment="Total request count across all models and sources")
    input_tokens = db.Column(db.BigInteger, default=0, nullable=False, comment="Total input tokens consumed across all models and sources")
    output_tokens = db.Column(db.BigInteger, default=0, nullable=False, comment="Total output tokens produced across all models and sources")
    cost = db.Column(db.Numeric(12, 6), default=0, nullable=False, comment="Total cost in USD across all models and sources")
    last_used_at = db.Column(db.DateTime, nullable=True, comment="UTC timestamp of the most recent request by this entity")

    __table_args__ = (
        {"comment": "Aggregated usage totals per entity across all models and sources; updated on every proxied request"},
    )
