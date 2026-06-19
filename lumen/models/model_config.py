from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.timeutils import utcnow
from ..extensions import db


class ModelConfig(db.Model):
    """Configuration and metadata for a proxied AI model.

    One row per logical model name exposed to clients. Actual backend
    connectivity (URL, credential, health) lives in model_endpoints. Capability
    fields are informational and populated from config.yaml; they are surfaced
    in the UI but not enforced by the proxy.
    """

    __tablename__ = "model_configs"
    __table_args__ = {"comment": "Configuration and metadata for each AI model that Lumen can proxy"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    # Canonical model identifier exposed to clients (e.g. 'gpt-4o')
    model_name: Mapped[str] = mapped_column(db.String(128), unique=True, comment="Canonical model identifier exposed to clients, e.g. gpt-4o")
    # USD cost per one million input/output tokens; used to compute coins
    input_cost_per_million: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), comment="USD cost per one million input tokens")
    output_cost_per_million: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), comment="USD cost per one million output tokens")
    # USD cost per minute of audio; only set for speech-to-text (ASR) models
    audio_cost_per_minute: Mapped[Optional[Decimal]] = mapped_column(db.Numeric(12, 6), comment="USD cost per minute of audio; only set for speech-to-text models")
    # Inactive models are hidden from clients and cannot be used
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Inactive models are hidden and cannot be used")
    description: Mapped[Optional[str]] = mapped_column(db.Text, comment="Human-readable description shown in the UI")
    # Link to provider documentation shown in the UI
    url: Mapped[Optional[str]] = mapped_column(db.String(512), comment="Link to provider documentation")
    supports_function_calling: Mapped[Optional[bool]] = mapped_column(db.Boolean, comment="Whether the model supports tool/function-calling")
    # e.g. ["text", "image"]
    input_modalities: Mapped[Optional[Any]] = mapped_column(db.JSON, comment='Supported input types, e.g. ["text", "image"]')
    # e.g. ["text"]
    output_modalities: Mapped[Optional[Any]] = mapped_column(db.JSON, comment='Supported output types, e.g. ["text"]')
    # Total context window in tokens (input + output)
    context_window: Mapped[Optional[int]] = mapped_column(db.Integer, comment="Total context window in tokens (input + output)")
    max_output_tokens: Mapped[Optional[int]] = mapped_column(db.Integer, comment="Maximum tokens the model can generate per response")
    supports_reasoning: Mapped[Optional[bool]] = mapped_column(db.Boolean, comment="Whether the model exposes chain-of-thought reasoning tokens")
    # Training data cutoff in YYYY-MM format
    knowledge_cutoff: Mapped[Optional[str]] = mapped_column(db.String(7), comment="Training data cutoff in YYYY-MM format")
    # Optional admin notice displayed to users on the model detail page
    notice: Mapped[Optional[str]] = mapped_column(db.Text, comment="Optional admin notice displayed to users on the model detail page")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")

    endpoints: Mapped[list["ModelEndpoint"]] = relationship(backref="model_config", lazy="select", cascade="all, delete-orphan", passive_deletes=True)
    stats: Mapped[list["ModelStat"]] = relationship(backref="model_config", lazy="select", passive_deletes=True)
