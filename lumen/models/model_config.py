from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.timeutils import utcnow

from ..extensions import db
from .model_alias import ModelAlias

#: The output modality that marks an embedding model. Sync writes it for a
#: backend that reports is_embedding, and config.yaml can set it by hand:
#: ``output_modalities: ["embedding"]``.
EMBEDDING_MODALITY = "embedding"


def is_embedding_model(config) -> bool:
    """True when the model produces embeddings rather than text.

    Takes a ModelConfig, a model_cache.ModelSnapshot or anything else with an
    ``output_modalities`` attribute. POST ``/v1/chat/completions``,
    ``/v1/completions``, ``/v1/messages``, ``/v1/audio/transcriptions`` and
    ``/v1/audio/translations`` refuse an embedding model with a 400; the web
    chat hides it and the OpenCode config leaves it out. ``/v1/embeddings``
    and ``/v1/messages/count_tokens`` do not check the marker.
    """
    value = getattr(config, "output_modalities", None)
    # Only the list (DB) and tuple (snapshot) shapes can hold the marker; a
    # scalar or a string stored by an older config is simply not one.
    return isinstance(value, (list, tuple)) and EMBEDDING_MODALITY in value


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
    # USD cost per one million input tokens served from the provider's prompt cache.
    # NULL = charge the input rate, so billing is unchanged until an admin sets it.
    cached_input_cost_per_million: Mapped[Optional[Decimal]] = mapped_column(db.Numeric(12, 6), comment="USD cost per one million cached input tokens; NULL = same as input_cost_per_million")
    # USD cost per one million input tokens written into the provider's prompt cache.
    # NULL = 0: written tokens are free until an admin sets a rate.
    cache_write_cost_per_million: Mapped[Optional[Decimal]] = mapped_column(db.Numeric(12, 6), comment="USD cost per one million cache-write input tokens; NULL = 0 (cache writes are free)")
    # USD cost per hour of audio; only set for speech-to-text (ASR) models
    audio_cost_per_hour: Mapped[Optional[Decimal]] = mapped_column(db.Numeric(12, 6), comment="USD cost per hour of audio; only set for speech-to-text models")
    # Owning user entity; NULL means the model is available to everyone.
    # ondelete=SET NULL: deleting the owner makes the model public again.
    owner_entity_id: Mapped[Optional[int]] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="SET NULL"), comment="Owning user entity; NULL = available to everyone. When set, only the owner may use the model")
    # Requires user acknowledgement before use; model-level only, not overridable by scopes
    needs_ack: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False, server_default=db.false(), comment="Requires user acknowledgement before use; sticky model-level property, not overridable by scopes")
    # Per-model acknowledgement message; overrides the global defaults.models.ack_message
    ack_message: Mapped[Optional[str]] = mapped_column(db.Text, comment="Per-model acknowledgement message; overrides the global defaults.models.ack_message")
    # Early-access model: users must acknowledge it may change or be removed
    early_access: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False, server_default=db.false(), comment="Early-access model: requires acknowledgement that it may change or be removed; sticky model-level property")
    # Naive-UTC datetime after which the model is hidden and rejected (exclusive); NULL = no end date
    end_date: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="Naive-UTC datetime after which the model is hidden and rejected (exclusive); NULL = no end date")
    # Hard off: hidden everywhere and cannot be overridden by any scope
    disabled: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False, server_default=db.false(), comment="Hard off: hidden everywhere and not overridable by any scope")
    description: Mapped[Optional[str]] = mapped_column(db.Text, comment="Human-readable description shown in the UI")
    # Link to provider documentation shown in the UI
    url: Mapped[Optional[str]] = mapped_column(db.String(512), comment="Link to provider documentation")
    supports_function_calling: Mapped[Optional[bool]] = mapped_column(db.Boolean, comment="Whether the model supports tool/function-calling")
    # e.g. ["text", "image"]
    input_modalities: Mapped[Optional[Any]] = mapped_column(db.JSON, comment='Supported input types, e.g. ["text", "image"]')
    # e.g. ["text"]; ["embedding"] marks an embedding model
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
    aliases: Mapped[list["ModelAlias"]] = relationship(backref="model_config", lazy="select", cascade="all, delete-orphan", passive_deletes=True)
    owner: Mapped[Optional["Entity"]] = relationship(foreign_keys=[owner_entity_id])

    @hybrid_property
    def active(self) -> bool:
        """A model is active (visible and usable) unless it is disabled or past its end date."""
        return not self.disabled and (self.end_date is None or self.end_date > utcnow())

    @active.expression
    def active(cls):
        return db.and_(cls.disabled == False,  # noqa: E712
                       db.or_(cls.end_date.is_(None), cls.end_date > utcnow()))
