import sqlalchemy as sa

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

    id = db.Column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="Surrogate PK; avoids timestamp collision under concurrent load",
    )
    # TimescaleDB partition key; kept non-unique to prevent collisions between concurrent workers.
    time = db.Column(db.DateTime(timezone=True), nullable=False, index=False, comment="UTC request timestamp; TimescaleDB partition key")
    # SET NULL on delete so historical data is preserved after entity removal
    entity_id = db.Column(
        db.Integer,
        db.ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
        comment="Requesting entity; SET NULL on delete to preserve historical data",
    )
    # SET NULL on delete so historical data is preserved after model removal
    model_config_id = db.Column(
        db.Integer,
        db.ForeignKey("model_configs.id", ondelete="SET NULL"),
        nullable=True,
        comment="Model used; SET NULL on delete to preserve historical data",
    )
    # SET NULL on delete so historical data is preserved after endpoint removal
    model_endpoint_id = db.Column(
        db.Integer,
        db.ForeignKey("model_endpoints.id", ondelete="SET NULL"),
        nullable=True,
        comment="Backend endpoint that served the request; SET NULL on delete to preserve historical data",
    )
    # 'chat' (web UI) or 'api' (API key)
    source = db.Column(db.String(8), nullable=False, comment="Origin of the request: chat (web UI) or api (API key)")
    input_tokens = db.Column(db.Integer, nullable=False, default=0, comment="Input token count for this request")
    output_tokens = db.Column(db.Integer, nullable=False, default=0, comment="Output token count for this request")
    cost = db.Column(db.Numeric(12, 6), nullable=False, default=0, comment="Cost in USD for this request")
    # Total proxy response time in seconds
    duration = db.Column(db.Float, nullable=False, default=0.0, comment="Total proxy response time in seconds")
