"""Add is_owner column to group_members

Revision ID: aa1b2c3d4e5f
Revises: e9f0a1b2c3d4
Create Date: 2026-08-17 00:00:00.000000

Adds a boolean ``is_owner`` column to ``group_members`` so a group can have a
designated owner — a member who can additionally add/remove other members,
transfer ownership, grant models they own, and activate/deactivate the group.
At most one owner per group is enforced by app logic.

SQLite dev uses ``create_all`` + stamp head and never runs this chain, so
only the PostgreSQL path needs to be correct (matching d5e6f7a8b9c0).
"""

import sqlalchemy as sa
from alembic import op

revision = "aa1b2c3d4e5f"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("group_members") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_owner",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN group_members.is_owner IS "
            f"{_q('True for the group owner; at most one owner per group (enforced by app logic)')}"
        )


def downgrade():
    with op.batch_alter_table("group_members") as batch_op:
        batch_op.drop_column("is_owner")
