from datetime import datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class RequestLog(db.Model):
    """Append-only log of every proxied request.

    On PostgreSQL this table is converted to a TimescaleDB hypertable
    partitioned by time, enabling efficient time-range queries and retention
    policies. On SQLite it behaves as a plain table.

    FK columns use SET NULL on delete to preserve historical records when
    entities, models, or endpoints are removed.
    """

    __tablename__ = "request_logs"
    __table_args__ = (
        db.Index("ix_request_logs_time", "time"),
        db.Index("ix_request_logs_entity_id", "entity_id"),
        db.Index("ix_request_logs_model_config_id", "model_config_id"),
        {"comment": "Append-only request log; TimescaleDB hypertable on PostgreSQL, plain table on SQLite"},
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="Surrogate PK; avoids timestamp collision under concurrent load",
    )
    # TimescaleDB partition key; kept non-unique to prevent collisions between concurrent workers.
    time: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), index=False, comment="UTC request timestamp; TimescaleDB partition key")
    # SET NULL on delete so historical data is preserved after entity removal
    entity_id: Mapped[Optional[int]] = mapped_column(
        db.Integer,
        db.ForeignKey("entities.id", ondelete="SET NULL"),
        comment="Requesting entity; SET NULL on delete to preserve historical data",
    )
    # SET NULL on delete so historical data is preserved after model removal
    model_config_id: Mapped[Optional[int]] = mapped_column(
        db.Integer,
        db.ForeignKey("model_configs.id", ondelete="SET NULL"),
        comment="Model used; SET NULL on delete to preserve historical data",
    )
    # SET NULL on delete so historical data is preserved after endpoint removal
    model_endpoint_id: Mapped[Optional[int]] = mapped_column(
        db.Integer,
        db.ForeignKey("model_endpoints.id", ondelete="SET NULL"),
        comment="Backend endpoint that served the request; SET NULL on delete to preserve historical data",
    )
    # 'chat' (web UI) or 'api' (API key)
    source: Mapped[str] = mapped_column(db.String(8), comment="Origin of the request: chat (web UI) or api (API key)")
    input_tokens: Mapped[int] = mapped_column(db.Integer, default=0, comment="Input token count for this request")
    output_tokens: Mapped[int] = mapped_column(db.Integer, default=0, comment="Output token count for this request")
    cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Cost in USD for this request")
    # Seconds of audio transcribed/translated for speech-to-text requests; 0 for text requests
    audio_seconds: Mapped[int] = mapped_column(db.Integer, default=0, comment="Seconds of audio transcribed/translated; 0 for text requests")
    # Total proxy response time in seconds
    duration: Mapped[float] = mapped_column(db.Float, default=0.0, comment="Total proxy response time in seconds")
