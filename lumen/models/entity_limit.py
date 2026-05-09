from ..extensions import db


class EntityLimit(db.Model):
    """Coin budget configuration for a single entity.

    One row per entity (enforced by the unique constraint on entity_id).
    Coin semantics: -2 = unlimited, 0 = blocked, positive = spendable budget.
    """

    __tablename__ = "entity_limits"
    __table_args__ = {"comment": "Coin budget configuration per entity; -2=unlimited, 0=blocked, positive=budget"}

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, unique=True, comment="The entity this limit applies to; one row per entity")
    # -2 = unlimited; 0 = blocked; positive = coin budget ceiling
    max_coins = db.Column(db.Numeric(12, 6), default=0, nullable=False, comment="Maximum coins the entity may hold; -2=unlimited, 0=blocked")
    # Coins credited on each periodic refill
    refresh_coins = db.Column(db.Numeric(12, 6), default=0, nullable=False, comment="Coins added on each periodic refill cycle")
    # Coins granted at entity creation or after a balance reset
    starting_coins = db.Column(db.Numeric(12, 6), default=0, nullable=False, comment="Coins granted at entity creation or after a balance reset")
    # When true, this row is owned by config.yaml and must not be edited via the UI
    config_managed = db.Column(db.Boolean, default=False, nullable=False, comment="When true, owned by config.yaml and must not be edited via the UI")
