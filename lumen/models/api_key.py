from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from ..extensions import db

KEY_TYPE_NORMAL = "normal"
KEY_TYPE_ADMIN = "admin"
KEY_TYPE_MONITORING = "monitoring"


class APIKey(db.Model):
    """API keys that entities use to authenticate against the proxy.

    The raw key is shown once at creation and never stored; only its keyed
    hash (HMAC-SHA256 under ENCRYPTION_KEY, see hash_api_key) is persisted. Cumulative usage counters (requests, tokens, cost) are
    incremented on each proxied request.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        db.Index("ix_api_keys_entity_id", "entity_id"),
        db.Index("ix_api_keys_expires_at", "expires_at"),
        db.CheckConstraint("key_type IN ('normal', 'admin', 'monitoring')", name="ck_api_keys_key_type"),
        {"comment": "API keys for entity authentication; only an HMAC-SHA256 of the key is stored, never the plaintext"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    uuid: Mapped[UUID] = mapped_column(db.Uuid, unique=True, index=True, nullable=False, default=uuid4, comment="Opaque public identifier for external use only (URLs, API payloads, CLI, session); never used as a key or join column")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="Owning entity")
    name: Mapped[str] = mapped_column(db.String(128), default="", comment="Human-readable label for the key")
    key_hash: Mapped[str] = mapped_column(db.String(64), unique=True, comment="HMAC-SHA256 of the raw key, keyed on ENCRYPTION_KEY")
    # Last few characters of the raw key shown in the UI for identification
    key_hint: Mapped[Optional[str]] = mapped_column(db.String(32), comment="Last few characters of the raw key shown in the UI for identification")
    key_type: Mapped[str] = mapped_column(db.String(16), nullable=False, default=KEY_TYPE_NORMAL, server_default=KEY_TYPE_NORMAL, comment="Key type: 'normal' (proxy API, billed), 'admin' (admin API and model listing) or 'monitoring' (model listing only); the type decides access, never the key prefix")
    revoked_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC time the key was revoked; null while the key is usable; approximate for keys revoked before this column existed")
    expires_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC time the key expires and is revoked (revoked_at set to this value); null if the key never expires")
    requests: Mapped[int] = mapped_column(db.Integer, default=0, comment="Cumulative request count made with this key")
    input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Cumulative input tokens consumed via this key")
    output_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Cumulative output tokens produced via this key")
    cached_input_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Cumulative cached-input part of input_tokens via this key (NULL request_logs counts add nothing)")
    cache_write_tokens: Mapped[int] = mapped_column(db.BigInteger, default=0, server_default="0", comment="Cumulative cache-write part of input_tokens via this key (NULL request_logs counts add nothing)")
    audio_seconds: Mapped[int] = mapped_column(db.BigInteger, default=0, comment="Cumulative seconds of audio transcribed/translated via this key")
    cost: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Cumulative cost in USD charged through this key")
    # Null if the key has never been used
    last_used_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp of the most recent request; null if never used")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")
    # Provenance: set only when the key was minted through an OAuth key request
    client_id: Mapped[Optional[str]] = mapped_column(db.String(128), comment="Application label from the OAuth request that minted this key; null for manually created keys")
    requested_by: Mapped[Optional[str]] = mapped_column(db.String(128), comment="Requester label from the OAuth request that minted this key; null for manually created keys")
    # Creator identity: a real FK, unlike the unverified requested_by label
    created_by_entity_id: Mapped[Optional[int]] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="SET NULL"), comment="Entity (user) that created this key; null for legacy keys or if the creator was deleted")
    revoked_by_entity_id: Mapped[Optional[int]] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="SET NULL"), comment="Entity (user) that revoked this key; null while usable, for keys revoked before this column existed, for system revocations (OAuth code replay, key expiry), or if the revoker was deleted")
