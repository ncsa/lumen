from ..extensions import db


class GroupMember(db.Model):
    """Association between an entity and a group.

    An entity may belong to multiple groups. config_managed=True rows were
    created by config.yaml and must not be removed through the UI.
    """

    __tablename__ = "group_members"

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, comment="The group")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The member entity")
    # When true, this membership was created by config.yaml and must not be removed via the UI
    config_managed = db.Column(db.Boolean, default=False, nullable=False, comment="When true, created by config.yaml and must not be removed via the UI")

    entity = db.relationship("Entity", backref="group_memberships")

    __table_args__ = (
        db.UniqueConstraint("group_id", "entity_id"),
        {"comment": "Association between entities and groups; an entity may belong to multiple groups"},
    )
