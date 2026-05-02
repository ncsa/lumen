from ..extensions import db


class GroupModelAccess(db.Model):
    __tablename__ = "group_model_access"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False)
    access_type = db.Column(db.String(20), nullable=False)  # 'whitelist' | 'blacklist' | 'graylist'

    model_config = db.relationship("ModelConfig")

    __table_args__ = (
        db.UniqueConstraint("group_id", "model_config_id", name="uq_gma_group_model"),
    )
