from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class GroupLimit(db.Model):
    """Coin budget configuration for a group.

    One row per group (enforced by the unique constraint on group_id).
    Applies to all group members unless overridden by an entity_limit row.
    Coin semantics: -2 = unlimited, 0 = blocked, positive = spendable budget.
    """

    __tablename__ = "group_limits"
    __table_args__ = {"comment": "Coin budget configuration per group; -2=unlimited, 0=blocked, positive=budget"}

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    group_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), unique=True, comment="The group this limit applies to; one row per group")
    # -2 = unlimited; 0 = blocked; positive = coin budget ceiling
    max_coins: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Maximum coins the group may hold; -2=unlimited, 0=blocked")
    # Coins credited on each periodic refill
    refresh_coins: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Coins added on each periodic refill cycle")
    # Coins granted at group creation or after a balance reset
    starting_coins: Mapped[Decimal] = mapped_column(db.Numeric(12, 6), default=0, comment="Coins granted at group creation or after a balance reset")
