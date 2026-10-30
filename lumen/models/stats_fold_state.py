from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


class StatsFoldState(db.Model):
    """Single-row state of the job that folds request_logs into the stats tables.

    Until ``cutover_at`` is set, billing keeps bumping ``model_stats`` and
    ``entity_stats`` per request and the fold does nothing. From ``cutover_at``
    on, the fold adds ``request_logs`` rows into those tables instead, window by
    half-open window, and ``folded_until`` is the end of the last folded window.
    A missing row means the same as a NULL ``cutover_at``: not cut over yet.
    """

    __tablename__ = "stats_fold_state"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, autoincrement=False, comment="Always 1; the table holds a single row")
    cutover_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC instant from which request_logs rows are folded instead of bumped by billing; null until cutover")
    folded_until: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC end (exclusive) of the last folded window; starts equal to cutover_at, null until cutover")

    __table_args__ = (
        {"comment": "Single-row state of the stats fold: cutover instant and folding watermark for model_stats/entity_stats"},
    )
