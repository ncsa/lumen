from ..extensions import db


class EntityManager(db.Model):
    __tablename__ = "entity_managers"
    __table_args__ = (
        db.UniqueConstraint("user_entity_id", "service_entity_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_entity_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    service_entity_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)

    user = db.relationship("Entity", foreign_keys=[user_entity_id], backref="managed_services_assoc")
    service = db.relationship("Entity", foreign_keys=[service_entity_id], backref="manager_assoc")
