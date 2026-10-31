"""Add request_logs.stats_deferred and cut the stats fold over automatically

Revision ID: f5b6c7d8e9f0
Revises: e4f5a6b7c8d9
Create Date: 2026-10-31 00:00:00.000000

Billing now records in each ``request_logs`` row what it did with the
``model_stats`` / ``entity_stats`` totals: TRUE when it skipped the bumps and
left the row to the stats fold, NULL when it bumped them. The fold adds only
TRUE rows. During a rolling update the older pods keep bumping and write NULL
(they do not know the column), so every request is counted exactly once
whatever the timing, and the cutover needs no operator step.

The column is nullable with no default: a catalogue-only change that rewrites
no data and works on a hypertable with compressed chunks (``i3j4k5l6m7n8``).

On PostgreSQL this revision also sets the cutover, ``cutover_at = folded_until
= now() - 1 hour``, unless one is set already. New-version processes start only
after ``flask db upgrade`` has finished, so they mark from their first request.
The hour of margin only means the first fold scans an hour of unmarked rows,
which it ignores, and keeps clock skew from stamping a marked row before
``folded_until``. SQLite was cut over by ``e4f5a6b7c8d9``.
"""

import sqlalchemy as sa
from alembic import op

revision = "f5b6c7d8e9f0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None

_COMMENT = (
    "TRUE when billing left this request out of model_stats/entity_stats for the "
    "stats fold to add; NULL when billing counted it directly"
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    # Nullable and without a server default, like f7a8b9c0d1e2: a default would
    # backfill every existing row, and only a NOT NULL column needs one.
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.add_column(sa.Column("stats_deferred", sa.Boolean(), nullable=True))

    if _is_postgresql():
        op.execute(f"COMMENT ON COLUMN request_logs.stats_deferred IS '{_COMMENT}'")
        op.execute(
            "UPDATE stats_fold_state "
            "SET cutover_at = (now() AT TIME ZONE 'UTC') - interval '1 hour', "
            "folded_until = (now() AT TIME ZONE 'UTC') - interval '1 hour' "
            "WHERE id = 1 AND cutover_at IS NULL"
        )


def downgrade():
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.drop_column("stats_deferred")
    if _is_postgresql():
        # Without the marker the fold cannot tell bumped rows from deferred ones,
        # so PostgreSQL goes back to "not cut over": billing bumps every request.
        op.execute("UPDATE stats_fold_state SET cutover_at = NULL, folded_until = NULL WHERE id = 1")
