"""Record when each group membership was created

Revision ID: ad4e5f6a7b8c
Revises: ac3d4e5f6a7b
Create Date: 2026-08-19 12:00:00.000000

Adds group_members.joined_at (naive UTC). Existing rows stay NULL — their
join time is unknown and backfilling "now" would be a lie; the UI shows a
dash for them.

SQLite dev uses ``create_all`` + stamp head and never runs this chain, so
only the PostgreSQL path needs to be correct.
"""

import sqlalchemy as sa
from alembic import op

revision = "ad4e5f6a7b8c"
down_revision = "ac3d4e5f6a7b"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("group_members") as batch_op:
        batch_op.add_column(sa.Column("joined_at", sa.DateTime(), nullable=True))
    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN group_members.joined_at IS "
            f"{_q('UTC timestamp when the membership was created; null for memberships that predate this column')}"
        )


def downgrade():
    with op.batch_alter_table("group_members") as batch_op:
        batch_op.drop_column("joined_at")
