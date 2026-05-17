from datetime import datetime, timezone

from lumen.extensions import db


class Conversation(db.Model):
    """Chat session created through the Lumen web UI.

    Each conversation belongs to one entity and contains an ordered list of
    messages. Setting hidden=True soft-deletes the conversation without
    removing messages from the database.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        db.Index("ix_conversations_entity_hidden_updated", "entity_id", "hidden", "updated_at"),
        {"comment": "Chat sessions created through the Lumen web UI; hidden=true is a soft delete"},
    )

    id = db.Column(db.Integer, primary_key=True, comment="Primary key")
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, comment="The owning user entity")
    title = db.Column(db.String(40), nullable=False, default="", comment="Short auto-generated or user-edited title")
    # Snapshot of the model name at conversation creation time
    model = db.Column(db.String(128), nullable=False, default="", comment="Snapshot of the model name at conversation creation time")
    # Soft-delete flag; hidden conversations are not shown in the UI
    hidden = db.Column(db.Boolean, nullable=False, default=False, comment="Soft-delete flag; hidden conversations are not shown in the UI")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC creation timestamp")
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="UTC timestamp of the most recent message or edit")

    messages = db.relationship(
        "Message", backref="conversation", lazy=True, cascade="all, delete-orphan", passive_deletes=True
    )
