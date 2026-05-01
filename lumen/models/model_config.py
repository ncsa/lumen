from datetime import datetime
from ..extensions import db


class ModelConfig(db.Model):
    __tablename__ = "model_configs"

    id = db.Column(db.Integer, primary_key=True)
    model_name = db.Column(db.String(128), unique=True, nullable=False)
    input_cost_per_million = db.Column(db.Numeric(12, 6), nullable=False)
    output_cost_per_million = db.Column(db.Numeric(12, 6), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    url = db.Column(db.String(512), nullable=True)
    max_input_tokens = db.Column(db.Integer, nullable=True)
    supports_function_calling = db.Column(db.Boolean, nullable=True)
    supports_vision = db.Column(db.Boolean, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    endpoints = db.relationship("ModelEndpoint", backref="model_config", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True)
    stats = db.relationship("ModelStat", backref="model_config", lazy="dynamic", passive_deletes=True)
