from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from ..extensions import db


class ModelAlias(db.Model):
    """Backward-compatible alias name pointing at a canonical ModelConfig.

    A client that requests an alias (e.g. ``glm-5.2``) is routed to the
    canonical model it points at (e.g. ``glm-5.3-flash``) so old names keep
    working after a model upgrade. Aliases do not grant access or copy consent;
    the canonical model supplies all policy and capability information, so usage
    is recorded under the canonical model_config_id and history is preserved.
    """

    __tablename__ = "model_aliases"
    __table_args__ = (
        db.Index("ix_model_aliases_model_config_id", "model_config_id"),
        {"comment": "Backward-compatible alias names that resolve to a canonical model_configs row"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    # The name clients may request; must be unique and must not collide with a canonical model name.
    # ondelete=CASCADE: deleting the canonical model removes its aliases.
    alias: Mapped[str] = mapped_column(db.String(128), unique=True, comment="Alias name clients may request; unique and never equal to any canonical model name")
    # Target canonical model. ondelete=CASCADE: an alias cannot outlive its target.
    model_config_id: Mapped[int] = mapped_column(db.Integer, db.ForeignKey("model_configs.id", ondelete="CASCADE"), comment="Canonical model_config this alias resolves to")
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, comment="UTC timestamp when the alias was created")
