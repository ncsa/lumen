from ..extensions import db


class GroupMember(db.Model):
    __tablename__ = "group_members"
    __table_args__ = (
        db.UniqueConstraint("group_id", "entity_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)

    entity = db.relationship("Entity", backref="group_memberships")
