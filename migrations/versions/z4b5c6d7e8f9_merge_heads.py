"""Merge divergent migration heads

Revision ID: z4b5c6d7e8f9
Revises: a4b5c6d7e8f9, z3a4b5c6d7e8
Create Date: 2026-06-05 00:00:00.000000

"""

from alembic import op

revision = "z4b5c6d7e8f9"
down_revision = ("a4b5c6d7e8f9", "z3a4b5c6d7e8")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
