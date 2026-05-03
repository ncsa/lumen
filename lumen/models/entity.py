from datetime import datetime, timezone
from ..extensions import db


class Entity(db.Model):
    __tablename__ = "entities"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(8), nullable=False)  # 'user' or 'client'
    email = db.Column(db.String(256), unique=True, nullable=True)  # users only
    name = db.Column(db.String(256), nullable=False)
    initials = db.Column(db.String(4), nullable=False, default="")
    gravatar_hash = db.Column(db.String(64), nullable=True)  # users only
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    model_access_default = db.Column(db.String(16), nullable=True)  # whitelist|blacklist|graylist; client entities only

    api_keys = db.relationship("APIKey", backref="entity", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    entity_limit = db.relationship("EntityLimit", backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    entity_balance = db.relationship("EntityBalance", backref="entity", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    model_stats = db.relationship("ModelStat", backref="entity", lazy="dynamic", passive_deletes=True)
