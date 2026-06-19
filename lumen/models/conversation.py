from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.timeutils import utcnow

from lumen.extensions import db


class Conversation(db.Model):
    """Chat session created through the Lumen web UI.

    Each conversation belongs to one entity and contains an ordered list of
    messages. Deleting a conversation hard-deletes it and all its messages.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        db.Index("ix_conversations_entity_updated", "entity_id", "updated_at"),
        {"comment": "Chat sessions created through the Lumen web UI"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    entity_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="The owning user entity")
    title: Mapped[str] = mapped_column(db.String(40), default="", comment="Short auto-generated or user-edited title")
    model: Mapped[str] = mapped_column(db.String(128), default="", comment="Snapshot of the model name at conversation creation time")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")
    updated_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp of the most recent message or edit")

    messages: Mapped[list["Message"]] = relationship(
        backref="conversation", lazy=True, cascade="all, delete-orphan", passive_deletes=True
    )
