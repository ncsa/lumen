from datetime import datetime, timezone
from ..extensions import db


class Entity(db.Model):
    """Unified table for human users (OAuth) and programmatic clients (API key).

    entity_type distinguishes the two kinds. Most columns apply to both; a few
    (email, gravatar_hash) are users-only and left null for clients.
    """

    __tablename__ = "entities"
    __table_args__ = {"comment": "Human users (OAuth) and programmatic clients (API key); entity_type distinguishes them"}

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    # 'user' for human users authenticated via OAuth; 'client' for API clients
    entity_type = db.Column(db.String(8), nullable=False, comment="'user' for OAuth users, 'client' for API clients")
    # Populated for users; null for clients. Unique across the table.
    email = db.Column(db.String(256), unique=True, nullable=True, comment="User email address; null for clients")
    name = db.Column(db.String(256), nullable=False, comment="Display name")
    initials = db.Column(db.String(4), nullable=False, default="", comment="Short initials for UI avatars")
    # MD5 hash of email for Gravatar lookups; users only
    gravatar_hash = db.Column(db.String(64), nullable=True, comment="MD5 hash of email for Gravatar lookups; users only")
    # Inactive entities are blocked from making any requests
    active = db.Column(db.Boolean, default=True, nullable=False, comment="Inactive entities are blocked from making requests")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")
    # Default access policy for models not explicitly listed in entity_model_access.
    # 'whitelist' | 'blacklist' | 'graylist'; primarily used for client entities.
    model_access_default = db.Column(db.String(16), nullable=True, comment="Default model access policy: whitelist, blacklist, or graylist; client entities only")

    api_keys = db.relationship("APIKey", backref="entity", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    entity_limit = db.relationship("EntityLimit", backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    entity_balance = db.relationship("EntityBalance", backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_stats = db.relationship("ModelStat", backref="entity", lazy="dynamic", passive_deletes=True)
