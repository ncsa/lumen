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
    __table_args__ = (
        # Every project has exactly one owner; users never have one.
        db.CheckConstraint(
            "(entity_type = 'project' AND owner_entity_id IS NOT NULL)"
            " OR (entity_type <> 'project' AND owner_entity_id IS NULL)",
            name="ck_entities_project_owner",
        ),
        # The owner must be a member of the project, and their membership row
        # cannot be deleted while they own it. Deferred so a project and its
        # owner's membership row can be inserted in one transaction; use_alter
        # because entity_managers references entities in turn.
        db.ForeignKeyConstraint(
            ["owner_entity_id", "id"],
            ["entity_managers.user_entity_id", "entity_managers.project_entity_id"],
            name="fk_entities_owner_membership",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        {"comment": "Human users (OAuth) and programmatic projects (API key); entity_type distinguishes them"},
    )

    # autoincrement is explicit: id is also part of fk_entities_owner_membership,
    # and SQLAlchemy does not auto-increment a primary key that is in a FK.
    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, autoincrement=True, comment="Primary key")
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
    # Owning user of a project; points at a manager row of that project
    owner_entity_id: Mapped[Optional[int]] = mapped_column(db.Integer, comment="Owning user; required for projects, null for users")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")

    # foreign_keys pinned to entity_id: the table also has created_by_entity_id
    api_keys: Mapped[list["APIKey"]] = relationship(backref="entity", lazy="select", cascade="all, delete-orphan", passive_deletes=True, foreign_keys="APIKey.entity_id")
    entity_limit: Mapped[Optional["EntityLimit"]] = relationship(backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    entity_balance: Mapped[Optional["EntityBalance"]] = relationship(backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_stats: Mapped[list["ModelStat"]] = relationship(backref="entity", lazy="select", passive_deletes=True)
