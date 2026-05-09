from datetime import datetime, timezone
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

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    # Canonical model identifier exposed to clients (e.g. 'gpt-4o')
    model_name = db.Column(db.String(128), unique=True, nullable=False, comment="Canonical model identifier exposed to clients, e.g. gpt-4o")
    # USD cost per one million input/output tokens; used to compute coins
    input_cost_per_million = db.Column(db.Numeric(12, 6), nullable=False, comment="USD cost per one million input tokens")
    output_cost_per_million = db.Column(db.Numeric(12, 6), nullable=False, comment="USD cost per one million output tokens")
    # Inactive models are hidden from clients and cannot be used
    active = db.Column(db.Boolean, default=True, nullable=False, comment="Inactive models are hidden and cannot be used")
    description = db.Column(db.Text, nullable=True, comment="Human-readable description shown in the UI")
    # Link to provider documentation shown in the UI
    url = db.Column(db.String(512), nullable=True, comment="Link to provider documentation")
    # Deprecated; prefer context_window
    max_input_tokens = db.Column(db.Integer, nullable=True, comment="Deprecated; use context_window instead")
    supports_function_calling = db.Column(db.Boolean, nullable=True, comment="Whether the model supports tool/function-calling")
    # e.g. ["text", "image"]
    input_modalities = db.Column(db.JSON, nullable=True, comment='Supported input types, e.g. ["text", "image"]')
    # e.g. ["text"]
    output_modalities = db.Column(db.JSON, nullable=True, comment='Supported output types, e.g. ["text"]')
    # Total context window in tokens (input + output)
    context_window = db.Column(db.Integer, nullable=True, comment="Total context window in tokens (input + output)")
    max_output_tokens = db.Column(db.Integer, nullable=True, comment="Maximum tokens the model can generate per response")
    supports_reasoning = db.Column(db.Boolean, nullable=True, comment="Whether the model exposes chain-of-thought reasoning tokens")
    # Training data cutoff in YYYY-MM format
    knowledge_cutoff = db.Column(db.String(7), nullable=True, comment="Training data cutoff in YYYY-MM format")
    # Optional admin notice displayed to users on the model detail page
    notice = db.Column(db.Text, nullable=True, comment="Optional admin notice displayed to users on the model detail page")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")

    endpoints = db.relationship("ModelEndpoint", backref="model_config", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    stats = db.relationship("ModelStat", backref="model_config", lazy="dynamic", passive_deletes=True)
