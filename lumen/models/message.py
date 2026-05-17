from datetime import datetime, timezone

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

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False,
        comment="The parent conversation"
    )
    # 'user', 'assistant', or 'system'
    role = db.Column(db.String(16), nullable=False, comment="Speaker role: user, assistant, or system")
    content = db.Column(db.Text, nullable=False, comment="Full message text")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")

    # Assistant-message metadata — null for user/system messages
    input_tokens = db.Column(db.Integer, nullable=True, comment="Input tokens reported by the model; assistant messages only")
    output_tokens = db.Column(db.Integer, nullable=True, comment="Output tokens reported by the model; assistant messages only")
    # Seconds from request send to first token received
    time_to_first_token = db.Column(db.Float, nullable=True, comment="Seconds from request send to first token received; assistant messages only")
    # Total response time in seconds
    duration = db.Column(db.Float, nullable=True, comment="Total response time in seconds; assistant messages only")
    # Output tokens per second
    output_speed = db.Column(db.Float, nullable=True, comment="Output tokens per second; assistant messages only")
    thinking = db.Column(db.Text, nullable=True, comment="Reasoning/thinking content from the model; assistant messages only")
    thinking_tokens = db.Column(db.Integer, nullable=True, comment="Thinking/reasoning token count reported by the model; assistant messages only")
