from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Mapped, aliased, mapped_column, relationship

from ..extensions import db
from .entity import Entity


class EntityManager(db.Model):
    """Maps a user to a project entity they are a member of.

    role is 'manager' (can view and administer the project's API keys and
    usage data) or 'user'. Both FKs reference the entities table;
    user_entity_id must be a 'user' entity and project_entity_id must be a
    'project' entity (enforced by app logic). The project's owner is the
    member that entities.owner_entity_id points at.
    """

    __tablename__ = "entity_managers"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    user_entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The user who has management rights over the project")
    project_entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The project entity being managed")
    role: Mapped[str] = mapped_column(db.String(16), nullable=False, default="manager", server_default="manager", comment="Project role: 'manager' or 'user'; the owner is the member entities.owner_entity_id points at")

    user: Mapped["Entity"] = relationship(foreign_keys=[user_entity_id], backref="managed_projects_assoc")
    project: Mapped["Entity"] = relationship(foreign_keys=[project_entity_id], backref="manager_assoc")

    __table_args__ = (
        db.UniqueConstraint("user_entity_id", "project_entity_id"),
        db.Index("ix_entity_managers_project_entity_id", "project_entity_id"),
        db.CheckConstraint("role IN ('manager', 'user')", name="ck_entity_managers_role"),
        {"comment": "Maps users to project entities they are permitted to manage"},
    )


def get_managed_projects(user_entity_id: int):
    """Project entities this user manages (active or not), ordered by name.

    Deactivated projects are included so a manager can still reach them
    and re-enable. Single join over EntityManager → Entity; returns Entity
    rows. Shared by the projects blueprint (access scoping) and the profile
    blueprint (Projects section).
    """
    return db.session.execute(
        select(Entity)
        .join(EntityManager, EntityManager.project_entity_id == Entity.id)
        .where(
            EntityManager.user_entity_id == user_entity_id,
            Entity.entity_type == "project",
        )
        .order_by(Entity.name)
    ).scalars().all()


def get_project_owner(project_entity_id: int):
    """The user Entity that owns this project (entities.owner_entity_id)."""
    project = aliased(Entity)
    return db.session.execute(
        select(Entity)
        .join(project, project.owner_entity_id == Entity.id)
        .where(project.id == project_entity_id)
    ).scalar_one_or_none()


def is_project_owner(user_entity_id: int, project_entity_id: int) -> bool:
    """True if user_entity_id is the owner of project_entity_id."""
    return db.session.scalar(
        select(Entity.id).where(
            Entity.id == project_entity_id,
            Entity.owner_entity_id == user_entity_id,
        )
    ) is not None


def get_project_role(user_entity_id: int, project_entity_id: int) -> Optional[Literal["owner", "manager", "user"]]:
    """The user's role in the project: 'owner', 'manager', 'user', or None.

    'owner' when entities.owner_entity_id points at the user; otherwise the
    role on their entity_managers row; None when they are not a member.
    """
    row = db.session.execute(
        select(EntityManager.role, Entity.owner_entity_id)
        .join(Entity, Entity.id == EntityManager.project_entity_id)
        .where(
            EntityManager.user_entity_id == user_entity_id,
            EntityManager.project_entity_id == project_entity_id,
        )
    ).one_or_none()
    if row is None:
        return None
    role, owner_entity_id = row
    return "owner" if owner_entity_id == user_entity_id else role
