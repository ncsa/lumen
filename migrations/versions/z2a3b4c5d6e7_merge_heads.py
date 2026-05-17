"""Merge divergent migration heads

Revision ID: z2a3b4c5d6e7
Revises: a1b2c3d4e5f7, a3b4c5d6e7f8, b2c3d4e5f6g7, z1b2c3d4e5f6
Create Date: 2026-05-17 00:00:00.000000

"""

from alembic import op

revision = "z2a3b4c5d6e7"
down_revision = ("a1b2c3d4e5f7", "a3b4c5d6e7f8", "b2c3d4e5f6g7", "z1b2c3d4e5f6")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
