from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db
from .entity import Entity


class EntityManager(db.Model):
    """Maps a user to a project entity they are permitted to manage.

    A manager can view and administer the project's API keys and usage data.
    Both FKs reference the entities table; user_entity_id must be a 'user'
    entity and project_entity_id must be a 'project' entity (enforced by app logic).
    """

    __tablename__ = "entity_managers"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    user_entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The user who has management rights over the project")
    project_entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The project entity being managed")
    is_owner: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False, comment="True for the project owner; at most one owner per project (enforced by app logic)")

    user: Mapped["Entity"] = relationship(foreign_keys=[user_entity_id], backref="managed_projects_assoc")
    project: Mapped["Entity"] = relationship(foreign_keys=[project_entity_id], backref="manager_assoc")

    __table_args__ = (
        db.UniqueConstraint("user_entity_id", "project_entity_id"),
        db.Index("ix_entity_managers_project_entity_id", "project_entity_id"),
        {"comment": "Maps users to project entities they are permitted to manage"},
    )


def get_managed_projects(user_entity_id: int):
    """Active project entities this user manages, ordered by name.

    Single join over EntityManager → Entity; returns Entity rows.
    Shared by the projects blueprint (access scoping) and the profile
    blueprint (Projects section).
    """
    return db.session.execute(
        select(Entity)
        .join(EntityManager, EntityManager.project_entity_id == Entity.id)
        .where(
            EntityManager.user_entity_id == user_entity_id,
            Entity.entity_type == "project",
            Entity.active == True,
        )
        .order_by(Entity.name)
    ).scalars().all()


def get_project_owner(project_entity_id: int):
    """The user Entity that owns this project, or None if no owner is set."""
    return db.session.execute(
        select(Entity)
        .join(EntityManager, EntityManager.user_entity_id == Entity.id)
        .where(
            EntityManager.project_entity_id == project_entity_id,
            EntityManager.is_owner == True,
        )
    ).scalar_one_or_none()


def is_project_owner(user_entity_id: int, project_entity_id: int) -> bool:
    """True if user_entity_id is the owner of project_entity_id."""
    assoc = db.session.execute(
        select(EntityManager).filter_by(
            user_entity_id=user_entity_id,
            project_entity_id=project_entity_id,
            is_owner=True,
        )
    ).scalar_one_or_none()
    return assoc is not None
