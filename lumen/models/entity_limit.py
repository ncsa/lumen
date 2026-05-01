from ..extensions import db


class EntityLimit(db.Model):
    __tablename__ = "entity_limits"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, unique=True)
    # -2 = unlimited; 0 = blocked; positive = coin budget
    max_coins = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    refresh_coins = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    starting_coins = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    config_managed = db.Column(db.Boolean, default=False, nullable=False)
