"""Add is_owner column to entity_managers

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-13 00:00:00.000000

Adds a boolean ``is_owner`` column to ``entity_managers`` so a project can
have a designated owner — a manager who can additionally add/remove other
managers, transfer ownership, and activate/deactivate the project. At most
one owner per project is enforced by app logic.

SQLite dev uses ``create_all`` + stamp head and never runs this chain, so
only the PostgreSQL path needs to be correct (matching c4d5e6f7a8b9).
"""

import sqlalchemy as sa
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("entity_managers") as batch_op:
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
            f"COMMENT ON COLUMN entity_managers.is_owner IS "
            f"{_q('True for the project owner; at most one owner per project (enforced by app logic)')}"
        )


def downgrade():
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.drop_column("is_owner")
