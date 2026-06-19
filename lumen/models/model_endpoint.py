from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow
from ..extensions import db


class ModelEndpoint(db.Model):
    """Backend endpoint for a model_config.

    A single model_config can have multiple endpoints for load distribution or
    failover. Lumen routes each request to a healthy endpoint, cycling through
    available ones.
    """

    __tablename__ = "model_endpoints"
    __table_args__ = (
        db.Index("ix_model_endpoints_model_config_id", "model_config_id"),
        {"comment": "Backend endpoints for model_configs; multiple endpoints enable load distribution and failover"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="Parent model configuration")
    # Base URL of the upstream API (e.g. https://api.openai.com/v1)
    url: Mapped[str] = mapped_column(db.String(256), comment="Base URL of the upstream API")
    # Credential forwarded to the upstream API
    api_key: Mapped[str] = mapped_column(db.String(256), comment="Credential forwarded to the upstream API")
    # When set, this name is sent to the endpoint instead of model_config.model_name,
    # allowing one Lumen model to fan out to endpoints that use different identifiers.
    model_name: Mapped[Optional[str]] = mapped_column(db.String(128), comment="Override model name sent upstream; null means use model_config.model_name")
    # Updated by the health-check background task
    healthy: Mapped[bool] = mapped_column(db.Boolean, default=False, comment="Last known health status, updated by the health-check background task")
    # Null if the endpoint has never been health-checked
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp of the most recent health check; null if never checked")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")
