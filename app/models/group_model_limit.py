from ..extensions import db


class GroupModelLimit(db.Model):
    __tablename__ = "group_model_limits"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    # nullable: NULL = default for all models
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id"), nullable=True)
    # -1 = defer to default; -2 = unlimited; 0 = blocked; positive = budget
    max_tokens = db.Column(db.BigInteger, default=-1, nullable=False)
    refresh_tokens = db.Column(db.Integer, default=0, nullable=False)
    starting_tokens = db.Column(db.BigInteger, default=0, nullable=False)

    model_config = db.relationship("ModelConfig")
