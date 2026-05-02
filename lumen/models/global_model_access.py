from ..extensions import db


class GlobalModelAccess(db.Model):
    __tablename__ = "global_model_access"

    id = db.Column(db.Integer, primary_key=True)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, unique=True)
    access_type = db.Column(db.String(20), nullable=False)  # 'whitelist' | 'blacklist' | 'graylist'

    model_config = db.relationship("ModelConfig")
