"""Add entity_managers.role ('manager' or 'user')

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-05 00:00:00.000000

Every existing membership row is a manager, so the column is added NOT NULL
with ``server_default='manager'`` and existing rows take that value. The CHECK
``ck_entity_managers_role`` limits it to ``'manager'`` and ``'user'``; the
project owner is still the member ``entities.owner_entity_id`` points at.
The ``entity_managers`` comments are reworded from "manage" to "member". The
downgrade deletes non-owner ``'user'`` rows rather than turning them into
managers; the owner's row is kept (``fk_entities_owner_membership`` forbids
deleting it), and an owner already has full rights.
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

# (column or None for the table, comment before this revision, comment after it)
_COMMENTS = [
    (None, "Maps users to project entities they are permitted to manage",
     "Maps users to the project entities they are members of"),
    ("user_entity_id", "The user who has management rights over the project",
     "The member user; their rights depend on role"),
    ("project_entity_id", "The project entity being managed",
     "The project the user is a member of"),
]


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def _set_comments(index):
    for column, *texts in _COMMENTS:
        target = "TABLE entity_managers" if column is None else f"COLUMN entity_managers.{column}"
        op.execute(f"COMMENT ON {target} IS {_q(texts[index])}")


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
        _set_comments(1)


def downgrade():
    # Before this revision every member was a manager. Keeping 'user' rows
    # would silently promote them, so they are removed instead -- except the
    # owner's own row, which fk_entities_owner_membership (RESTRICT) protects
    # and whose holder has full rights anyway.
    op.execute(sa.text(
        """
        DELETE FROM entity_managers
        WHERE role = 'user' AND NOT EXISTS (
            SELECT 1 FROM entities e
            WHERE e.id = entity_managers.project_entity_id
              AND e.owner_entity_id = entity_managers.user_entity_id
        )
        """
    ))
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.drop_constraint("ck_entity_managers_role", type_="check")
        batch_op.drop_column("role")

    if _is_postgresql():
        _set_comments(0)
