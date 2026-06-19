from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow
from ..extensions import db


class APIKey(db.Model):
    """API keys that entities use to authenticate against the proxy.

    The raw key is shown once at creation and never stored; only the SHA-256
    hash is persisted. Cumulative usage counters (requests, tokens, cost) are
    incremented on each proxied request.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        db.Index("ix_api_keys_entity_id", "entity_id"),
        {"comment": "API keys for entity authentication; only the SHA-256 hash is stored, never the plaintext"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="Owning entity")
    name: Mapped[str] = mapped_column(db.String(128), default="", comment="Human-readable label for the key")
    key_hash: Mapped[str] = mapped_column(db.String(64), unique=True, comment="SHA-256 hash of the raw key")
    # Last few characters of the raw key shown in the UI for identification
    key_hint: Mapped[Optional[str]] = mapped_column(db.String(32), comment="Last few characters of the raw key shown in the UI for identification")
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Inactive keys are rejected on all requests")
    requests: Mapped[int] = mapped_column(db.Integer, default=0, comment="Cumulative request count made with this key")
    input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Cumulative input tokens consumed via this key")
    output_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Cumulative output tokens produced via this key")
    audio_seconds: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Cumulative seconds of audio transcribed/translated via this key")
    cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Cumulative cost in USD charged through this key")
    # Null if the key has never been used
    last_used_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp of the most recent request; null if never used")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")
