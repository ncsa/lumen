from datetime import datetime

from lumen.extensions import db


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id"), nullable=False
    )
    role = db.Column(db.String(16), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Nullable metadata (assistant messages only)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    time_to_first_token = db.Column(db.Float, nullable=True)
    duration = db.Column(db.Float, nullable=True)
    output_speed = db.Column(db.Float, nullable=True)
