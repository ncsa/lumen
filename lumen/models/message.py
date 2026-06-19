from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from lumen.extensions import db


class Message(db.Model):
    """A single turn within a conversation.

    Both user and assistant messages are stored here. Performance and token
    metadata columns are only populated for assistant messages; they are null
    for user and system messages.
    """

    __tablename__ = "messages"
    __table_args__ = (
        db.Index("ix_messages_conversation_id", "conversation_id"),
        {"comment": "Individual turns within a conversation; performance metadata columns are assistant-only"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    conversation_id: Mapped[int] = mapped_column(
        db.Integer, db.ForeignKey("conversations.id", ondelete="CASCADE"),
        comment="The parent conversation"
    )
    # 'user', 'assistant', or 'system'
    role: Mapped[str] = mapped_column(db.String(16), comment="Speaker role: user, assistant, or system")
    content: Mapped[str] = mapped_column(db.Text, comment="Full message text")
    created_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")

    # Assistant-message metadata — null for user/system messages
    input_tokens: Mapped[Optional[int]] = mapped_column(db.Integer, comment="Input tokens reported by the model; assistant messages only")
    output_tokens: Mapped[Optional[int]] = mapped_column(db.Integer, comment="Output tokens reported by the model; assistant messages only")
    # Seconds from request send to first token received
    time_to_first_token: Mapped[Optional[float]] = mapped_column(db.Float, comment="Seconds from request send to first token received; assistant messages only")
    # Total response time in seconds
    duration: Mapped[Optional[float]] = mapped_column(db.Float, comment="Total response time in seconds; assistant messages only")
    # Output tokens per second
    output_speed: Mapped[Optional[float]] = mapped_column(db.Float, comment="Output tokens per second; assistant messages only")
    thinking: Mapped[Optional[str]] = mapped_column(db.Text, comment="Reasoning/thinking content from the model; assistant messages only")
    thinking_tokens: Mapped[Optional[int]] = mapped_column(db.Integer, comment="Thinking/reasoning token count reported by the model; assistant messages only")
