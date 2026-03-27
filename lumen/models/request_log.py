from ..extensions import db


class RequestLog(db.Model):
    __tablename__ = "request_logs"

    # time is used as the SQLAlchemy identity column; the table is append-only
    # so uniqueness is not enforced at the ORM level.
    time = db.Column(db.DateTime(timezone=True), primary_key=True, nullable=False)
    entity_id = db.Column(
        db.Integer,
        db.ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    model_config_id = db.Column(
        db.Integer,
        db.ForeignKey("model_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    model_endpoint_id = db.Column(
        db.Integer,
        db.ForeignKey("model_endpoints.id", ondelete="SET NULL"),
        nullable=True,
    )
    source = db.Column(db.String(8), nullable=False)  # 'chat' or 'api'
    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    cost = db.Column(db.Numeric(12, 6), nullable=False, default=0)
    duration = db.Column(db.Float, nullable=False, default=0.0)
