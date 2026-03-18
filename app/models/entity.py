from datetime import datetime
from ..extensions import db


class Entity(db.Model):
    __tablename__ = "entities"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(8), nullable=False)  # 'user' or 'service'
    email = db.Column(db.String(256), unique=True, nullable=True)  # users only
    name = db.Column(db.String(256), nullable=False)
    initials = db.Column(db.String(4), nullable=False, default="")
    gravatar_hash = db.Column(db.String(64), nullable=True)  # users only
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    api_keys = db.relationship("APIKey", backref="entity", lazy="dynamic")
    model_limits = db.relationship("ModelLimit", backref="entity", lazy="dynamic")
    model_stats = db.relationship("ModelStat", backref="entity", lazy="dynamic")
