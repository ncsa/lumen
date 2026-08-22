from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from ..extensions import db


class ModelGroupAccess(db.Model):
    """Group grant for an owned model.

    Presence of a row grants every member of the group access to the model.
    Rows only exist for models that have an owner; models without an owner
    are available to everyone and carry no grants.
    """

    __tablename__ = "model_group_access"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="The owned model being granted")
    group_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), comment="The group whose members are granted access")
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp when the grant was created")

    __table_args__ = (
        db.UniqueConstraint("model_config_id", "group_id", name="uq_mga_model_group"),
        db.Index("ix_model_group_access_group_id", "group_id"),
        {"comment": "Group grants for owned models; a row gives all group members access to the model"},
    )
