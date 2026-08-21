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
        # Composite on the analytics predicate (model + time). Created by
        # migration f7a8b9c0d1e2 on PostgreSQL only; declared here so the model
        # and a fresh create_all agree with an upgraded schema (no autogenerate
        # drift). Column names are the string form because __table_args__ is
        # evaluated before the mapped columns exist.
        db.Index("ix_request_logs_model_config_id_time", "model_config_id", sa.desc("time")),
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
    # Set when the client went away before the stream finished. The upstream
    # reports usage only in its terminal chunk, so an aborted request's token
    # counts are an estimate unless that chunk had already arrived. This replaces
    # the old "cost == 0 identifies an abort" convention, which stops being
    # unique once aborted requests are billed for what they consumed.
    aborted: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default=sa.false(),
        nullable=False,
        comment="Client disconnected before the stream completed; token counts may be estimated",
    )
    # --- Request timing -----------------------------------------------------
    #
    # `time` above is the COMPLETION timestamp (stamped after billing), and
    # `duration` starts only after preflight. Neither can be walked backwards to
    # the moment the request arrived: preflight and the billing commit are both
    # unmeasured, and both are largest exactly during a burst, when the pool is
    # contended. So arrival is stored outright rather than derived.
    #
    # started_at is TIMESTAMPTZ, deliberately unlike every other timestamp
    # column in the codebase (which are naive UTC per CLAUDE.md). It exists to
    # be compared and subtracted against `time` on the same row, and mixing
    # naive with aware in that arithmetic is a Postgres footgun. It must be
    # written with datetime.now(timezone.utc), NOT timeutils.utcnow(), which
    # returns naive and would be silently reinterpreted against the session
    # TimeZone.
    started_at: Mapped[Optional[datetime]] = mapped_column(
        db.DateTime(timezone=True),
        nullable=True,
        comment="UTC instant the request arrived at the ASGI bridge (T0). TIMESTAMPTZ to match `time` for same-row arithmetic; null for requests that did not pass through the bridge (dev server, test client) or predate this column",
    )
    queue_wait: Mapped[Optional[float]] = mapped_column(
        db.Float,
        nullable=True,
        comment="Seconds spent waiting for a WSGI worker thread (T1-T0). The queue Lumen itself owns, and the number that distinguishes 'we are under-provisioned' from 'the model is slow'",
    )
    preflight: Mapped[Optional[float]] = mapped_column(
        db.Float,
        nullable=True,
        comment="Seconds from worker pickup to the upstream call (T2-T1): auth, model lookup, coin budget, endpoint selection, DB pool checkout. NOTE: composition differs by path - on the audio path it is dominated by multipart upload parsing, and on the chat stream it spans two preflights",
    )
    ttft: Mapped[Optional[float]] = mapped_column(
        db.Float,
        nullable=True,
        comment="Seconds to the first upstream chunk of ANY kind, including reasoning deltas",
    )
    ttft_visible: Mapped[Optional[float]] = mapped_column(
        db.Float,
        nullable=True,
        comment="Seconds to the first visible content delta. On a reasoning model this trails ttft by the whole thinking phase, which is why both are stored: it separates 'the model was queued' from 'the model was thinking'",
    )
    send_blocked: Mapped[Optional[float]] = mapped_column(
        db.Float,
        nullable=True,
        comment="Seconds blocked handing response chunks to the server. A large share means a slow client, not a slow backend - duration conflates the two. 0 on non-streaming paths, where billing completes before the body is handed over and the value is unknowable",
    )
    # Only values the code can actually write. `billing_error` and
    # `upstream_error` are deliberately absent: the row is created inside
    # update_stats, which only flushes, so the commit that fails is the same one
    # that would have persisted the row recording the failure. Add a value only
    # together with the code that writes it.
    outcome: Mapped[Optional[str]] = mapped_column(
        db.String(16),
        nullable=True,
        comment="How the request ended: ok | disconnect. Null means the row predates this column - not 'unknown outcome'",
    )
