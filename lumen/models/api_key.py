from datetime import datetime, timezone
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

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="Owning entity")
    name = db.Column(db.String(128), nullable=False, default="", comment="Human-readable label for the key")
    # SHA-256 hash of the raw key; nullable only during legacy plaintext-to-hash migration
    key_hash = db.Column(db.String(64), unique=True, nullable=True, comment="SHA-256 hash of the raw key; null only during legacy migration")
    # Last few characters of the raw key shown in the UI for identification
    key_hint = db.Column(db.String(32), nullable=True, comment="Last few characters of the raw key shown in the UI for identification")
    active = db.Column(db.Boolean, default=True, nullable=False, comment="Inactive keys are rejected on all requests")
    requests = db.Column(db.Integer, default=0, nullable=False, comment="Cumulative request count made with this key")
    input_tokens = db.Column(db.BigInteger, default=0, nullable=False, comment="Cumulative input tokens consumed via this key")
    output_tokens = db.Column(db.BigInteger, default=0, nullable=False, comment="Cumulative output tokens produced via this key")
    cost = db.Column(db.Numeric(12, 6), default=0, nullable=False, comment="Cumulative cost in USD charged through this key")
    # Null if the key has never been used
    last_used_at = db.Column(db.DateTime, nullable=True, comment="UTC timestamp of the most recent request; null if never used")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")
