"""Add stats_fold_state, the cutover and watermark of the stats fold

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-30 00:00:00.000000

``model_stats`` and ``entity_stats`` move from a per-request bump in billing
to a background fold over ``request_logs``. This single-row table records when
that switch happens (``cutover_at``) and how far the fold has got
(``folded_until``).

On SQLite the row is cut over at migration time: a single process means no
old code can still be bumping the counters. On PostgreSQL both columns stay
NULL, so nothing changes until an operator cuts over after the rollout.
"""

import sqlalchemy as sa
from alembic import op

from lumen.timeutils import utcnow

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "stats_fold_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False, comment="Always 1; the table holds a single row"),
        sa.Column("cutover_at", sa.DateTime(), nullable=True, comment="UTC instant from which request_logs rows are folded instead of bumped by billing; null until cutover"),
        sa.Column("folded_until", sa.DateTime(), nullable=True, comment="UTC end (exclusive) of the last folded window; starts equal to cutover_at, null until cutover"),
        sa.PrimaryKeyConstraint("id"),
        comment="Single-row state of the stats fold: cutover instant and folding watermark for model_stats/entity_stats",
    )
    now = utcnow() if op.get_bind().dialect.name == "sqlite" else None
    op.bulk_insert(table, [{"id": 1, "cutover_at": now, "folded_until": now}])


def downgrade():
    op.drop_table("stats_fold_state")
