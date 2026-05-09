from datetime import datetime, timezone
from ..extensions import db


class EntityBalance(db.Model):
    """Current coin balance for a single entity.

    One row per entity. Decremented on each proxied request; replenished by
    the periodic refill job up to entity_limits.max_coins.
    """

    __tablename__ = "entity_balances"
    __table_args__ = {"comment": "Current coin balance per entity; decremented on requests, replenished by the refill job"}

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, unique=True, comment="The entity this balance belongs to; one row per entity")
    coins_left = db.Column(db.Numeric(12, 6), default=0, comment="Current spendable coin balance")
    last_refill_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC timestamp of the most recent coin refill")
