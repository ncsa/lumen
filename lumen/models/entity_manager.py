from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db


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

    user: Mapped["Entity"] = relationship(foreign_keys=[user_entity_id], backref="managed_projects_assoc")
    project: Mapped["Entity"] = relationship(foreign_keys=[project_entity_id], backref="manager_assoc")

    __table_args__ = (
        db.UniqueConstraint("user_entity_id", "project_entity_id"),
        db.Index("ix_entity_managers_project_entity_id", "project_entity_id"),
        {"comment": "Maps users to project entities they are permitted to manage"},
    )
