from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class GroupRule(db.Model):
    """One auto-join condition for a group.

    A group with auto_join enabled assigns membership at OAuth login to any
    user whose identity-provider claims match ALL of the group's rules. Each
    rule compares one userinfo field against a value, either by substring
    (contains) or exact match (equals). A group must have at least one rule
    for auto-join to have any effect (enforced by app logic — matching fails
    closed on an empty rule set).
    """

    __tablename__ = "group_rules"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    group_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), comment="The group this rule belongs to")
    field: Mapped[str] = mapped_column(db.String(128), comment="Userinfo claim to inspect (e.g. 'affiliation', 'idp')")
    # 'contains' = substring match; 'equals' = exact match
    match: Mapped[str] = mapped_column(db.String(8), comment="'contains' for substring match, 'equals' for exact match")
    value: Mapped[str] = mapped_column(db.String(256), comment="Value the claim is compared against")

    __table_args__ = (
        db.Index("ix_group_rules_group_id", "group_id"),
        {"comment": "Auto-join conditions per group; a user matching ALL of a group's rules is added at login"},
    )
