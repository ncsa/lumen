from datetime import datetime, timezone

from lumen.extensions import db


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    title = db.Column(db.String(40), nullable=False, default="")
    model = db.Column(db.String(128), nullable=False, default="")
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    messages = db.relationship(
        "Message", backref="conversation", lazy=True, cascade="all, delete-orphan", passive_deletes=True
    )
