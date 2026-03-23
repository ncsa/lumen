from datetime import datetime
from ..extensions import db


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    config_managed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    members = db.relationship("GroupMember", backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    limits = db.relationship("GroupModelLimit", backref="group", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
