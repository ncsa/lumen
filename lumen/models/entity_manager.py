from ..extensions import db


class EntityManager(db.Model):
    """Maps a user to a client entity they are permitted to manage.

    A manager can view and administer the client's API keys and usage data.
    Both FKs reference the entities table; user_entity_id must be a 'user'
    entity and client_entity_id must be a 'client' entity (enforced by app logic).
    """

    __tablename__ = "entity_managers"

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    user_entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The user who has management rights over the client")
    client_entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The client entity being managed")

    user = db.relationship("Entity", foreign_keys=[user_entity_id], backref="managed_clients_assoc")
    client = db.relationship("Entity", foreign_keys=[client_entity_id], backref="manager_assoc")

    __table_args__ = (
        db.UniqueConstraint("user_entity_id", "client_entity_id"),
        {"comment": "Maps users to client entities they are permitted to manage"},
    )
