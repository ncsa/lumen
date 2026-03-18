from datetime import datetime
from ..extensions import db


class ModelConfig(db.Model):
    __tablename__ = "model_configs"

    id = db.Column(db.Integer, primary_key=True)
    model_name = db.Column(db.String(128), unique=True, nullable=False)
    input_cost_per_million = db.Column(db.Numeric(12, 6), nullable=False)
    output_cost_per_million = db.Column(db.Numeric(12, 6), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    endpoints = db.relationship("ModelEndpoint", backref="model_config", lazy="dynamic")
    limits = db.relationship("ModelLimit", backref="model_config", lazy="dynamic")
    stats = db.relationship("ModelStat", backref="model_config", lazy="dynamic")
