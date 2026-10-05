"""Add entity_managers.role ('manager' or 'user')

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-05 00:00:00.000000

Every existing membership row is a manager, so the column is added NOT NULL
with ``server_default='manager'`` and existing rows take that value. The CHECK
``ck_entity_managers_role`` limits it to ``'manager'`` and ``'user'``; the
project owner is still the member ``entities.owner_entity_id`` points at.
The downgrade deletes ``'user'`` rows rather than turning them into managers.
batch_alter_table keeps the statements portable; SQLite dev uses
``create_all`` + stamp head and never runs this chain.
"""

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None

_ROLE_COMMENT = "Project role: 'manager' or 'user'; the owner is the member entities.owner_entity_id points at"


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.add_column(
            sa.Column(
                "role",
                sa.String(16),
                nullable=False,
                server_default="manager",
            )
        )
        batch_op.create_check_constraint(
            "ck_entity_managers_role",
            "role IN ('manager', 'user')",
        )

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN entity_managers.role IS "
            f"{_q(_ROLE_COMMENT)}"
        )


def downgrade():
    # Before this revision every member was a manager. Keeping 'user' rows
    # would silently promote them, so they are removed instead.
    op.execute(sa.text("DELETE FROM entity_managers WHERE role = 'user'"))
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.drop_constraint("ck_entity_managers_role", type_="check")
        batch_op.drop_column("role")
