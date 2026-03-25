from ..extensions import db


class GroupLimit(db.Model):
    __tablename__ = "group_limits"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, unique=True)
    # -2 = unlimited; 0 = blocked; positive = budget
    max_tokens = db.Column(db.BigInteger, default=0, nullable=False)
    refresh_tokens = db.Column(db.Integer, default=0, nullable=False)
    starting_tokens = db.Column(db.BigInteger, default=0, nullable=False)
