from datetime import datetime, timezone
from ..extensions import db


class ModelEndpoint(db.Model):
    """Backend endpoint for a model_config.

    A single model_config can have multiple endpoints for load distribution or
    failover. Lumen routes each request to a healthy endpoint, cycling through
    available ones.
    """

    __tablename__ = "model_endpoints"
    __table_args__ = {"comment": "Backend endpoints for model_configs; multiple endpoints enable load distribution and failover"}

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, comment="Parent model configuration")
    # Base URL of the upstream API (e.g. https://api.openai.com/v1)
    url = db.Column(db.String(256), nullable=False, comment="Base URL of the upstream API")
    # Credential forwarded to the upstream API
    api_key = db.Column(db.String(256), nullable=False, comment="Credential forwarded to the upstream API")
    # When set, this name is sent to the endpoint instead of model_config.model_name,
    # allowing one Lumen model to fan out to endpoints that use different identifiers.
    model_name = db.Column(db.String(128), nullable=True, comment="Override model name sent upstream; null means use model_config.model_name")
    # Updated by the health-check background task
    healthy = db.Column(db.Boolean, default=False, nullable=False, comment="Last known health status, updated by the health-check background task")
    # Null if the endpoint has never been health-checked
    last_checked_at = db.Column(db.DateTime, nullable=True, comment="UTC timestamp of the most recent health check; null if never checked")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")
