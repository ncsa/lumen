"""Add model_access_default to entities

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-05-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "r8s9t0u1v2w3"
down_revision = "q7r8s9t0u1v2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "entities",
        sa.Column("model_access_default", sa.String(16), nullable=True),
    )


def downgrade():
    op.drop_column("entities", "model_access_default")
