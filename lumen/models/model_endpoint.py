from datetime import datetime
from ..extensions import db


class ModelEndpoint(db.Model):
    __tablename__ = "model_endpoints"

    id = db.Column(db.Integer, primary_key=True)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False)
    url = db.Column(db.String(256), nullable=False)
    api_key = db.Column(db.String(256), nullable=False)
    # If set, this name is sent to the endpoint instead of the parent ModelConfig.model_name,
    # allowing one Lumen model to fan out to endpoints that use different model identifiers.
    model_name = db.Column(db.String(128), nullable=True)
    healthy = db.Column(db.Boolean, default=False, nullable=False)
    last_checked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
