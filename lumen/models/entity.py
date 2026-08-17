from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.timeutils import utcnow
from ..extensions import db


class Entity(db.Model):
    """Unified table for human users (OAuth) and programmatic projects (API key).

    entity_type distinguishes the two kinds. Most columns apply to both; a few
    (email, gravatar_hash) are users-only and left null for projects.
    """

    __tablename__ = "entities"
    __table_args__ = {"comment": "Human users (OAuth) and programmatic projects (API key); entity_type distinguishes them"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    # 'user' for human users authenticated via OAuth; 'project' for API projects
    entity_type: Mapped[str] = mapped_column(db.String(8), comment="'user' for OAuth users, 'project' for API projects")
    # Populated for users; null for projects. Unique across the table.
    email: Mapped[Optional[str]] = mapped_column(db.String(256), unique=True, comment="User email address; null for projects")
    name: Mapped[str] = mapped_column(db.String(256), comment="Display name")
    initials: Mapped[str] = mapped_column(db.String(4), default="", comment="Short initials for UI avatars")
    # MD5 hash of email for Gravatar lookups; users only
    gravatar_hash: Mapped[Optional[str]] = mapped_column(db.String(64), comment="MD5 hash of email for Gravatar lookups; users only")
    # Inactive entities are blocked from making any requests
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Inactive entities are blocked from making requests")
    # When False, webchat conversations are not persisted for this user
    store_conversations: Mapped[bool] = mapped_column(db.Boolean, default=True, comment="Whether webchat conversations are persisted for this user")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")

    api_keys: Mapped[list["APIKey"]] = relationship(backref="entity", lazy="select", cascade="all, delete-orphan", passive_deletes=True)
    entity_limit: Mapped[Optional["EntityLimit"]] = relationship(backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    entity_balance: Mapped[Optional["EntityBalance"]] = relationship(backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_stats: Mapped[list["ModelStat"]] = relationship(backref="entity", lazy="select", passive_deletes=True)
