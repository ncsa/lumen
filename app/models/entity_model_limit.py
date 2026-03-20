from ..extensions import db


class EntityModelLimit(db.Model):
    __tablename__ = "entity_model_limits"

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    # nullable: NULL = default for all models
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id"), nullable=True)
    # -1 = defer to default; -2 = unlimited; 0 = blocked; positive = budget
    max_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    refresh_tokens = db.Column(db.Integer, default=0, nullable=False)
    starting_tokens = db.Column(db.BigInteger, default=0, nullable=False)
