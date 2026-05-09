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
    __table_args__ = {"comment": "Append-only request log; TimescaleDB hypertable on PostgreSQL, plain table on SQLite"}

    # Used as the SQLAlchemy PK and TimescaleDB partition key; the table is
    # append-only so ORM-level uniqueness is not enforced.
    time = db.Column(db.DateTime(timezone=True), primary_key=True, nullable=False, comment="UTC request timestamp; TimescaleDB partition key")
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
