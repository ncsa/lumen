from ..extensions import db


class EntityLimit(db.Model):
    __tablename__ = "entity_limits"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, unique=True)
    # -2 = unlimited; 0 = blocked; positive = budget
    max_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    refresh_tokens = db.Column(db.Integer, default=0, nullable=False)
    starting_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    config_managed = db.Column(db.Boolean, default=False, nullable=False)
