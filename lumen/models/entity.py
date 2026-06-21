from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.timeutils import utcnow
from ..extensions import db


class Entity(db.Model):
    """Unified table for human users (OAuth) and programmatic clients (API key).

    entity_type distinguishes the two kinds. Most columns apply to both; a few
    (email, gravatar_hash) are users-only and left null for clients.
    """

    __tablename__ = "entities"
    __table_args__ = {"comment": "Human users (OAuth) and programmatic clients (API key); entity_type distinguishes them"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    # 'user' for human users authenticated via OAuth; 'client' for API clients
    entity_type: Mapped[str] = mapped_column(db.String(8), comment="'user' for OAuth users, 'client' for API clients")
    # Populated for users; null for clients. Unique across the table.
    email: Mapped[Optional[str]] = mapped_column(db.String(256), unique=True, comment="User email address; null for clients")
    name: Mapped[str] = mapped_column(db.String(256), comment="Display name")
    initials: Mapped[str] = mapped_column(db.String(4), default="", comment="Short initials for UI avatars")
    # MD5 hash of email for Gravatar lookups; users only
    gravatar_hash: Mapped[Optional[str]] = mapped_column(db.String(64), comment="MD5 hash of email for Gravatar lookups; users only")
    # Inactive entities are blocked from making any requests
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Inactive entities are blocked from making requests")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")
    # Default access policy for models not explicitly listed in entity_model_access.
    # 'allowed' | 'blocked'; primarily used for client entities.
    model_access_default: Mapped[Optional[str]] = mapped_column(db.String(16), comment="Default model access policy: 'allowed' or 'blocked'; client entities only")

    api_keys: Mapped[list["APIKey"]] = relationship(backref="entity", lazy="select", cascade="all, delete-orphan", passive_deletes=True)
    entity_limit: Mapped[Optional["EntityLimit"]] = relationship(backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    entity_balance: Mapped[Optional["EntityBalance"]] = relationship(backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_stats: Mapped[list["ModelStat"]] = relationship(backref="entity", lazy="select", passive_deletes=True)
