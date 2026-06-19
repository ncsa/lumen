from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow
from ..extensions import db


class EntityBalance(db.Model):
    """Current coin balance for a single entity.

    One row per entity. Decremented on each proxied request; replenished by
    the periodic refill job up to entity_limits.max_coins.
    """

    __tablename__ = "entity_balances"
    __table_args__ = {"comment": "Current coin balance per entity; decremented on requests, replenished by the refill job"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), unique=True, comment="The entity this balance belongs to; one row per entity")
    coins_left: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Current spendable coin balance")
    last_refill_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp of the most recent coin refill")
