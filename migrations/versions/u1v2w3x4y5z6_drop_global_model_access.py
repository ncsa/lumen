"""Drop global_model_access table

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-05-04 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op


revision = "u1v2w3x4y5z6"
down_revision = "t0u1v2w3x4y5"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("global_model_access")


def downgrade():
    op.create_table(
        "global_model_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("access_type", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_config_id"),
    )
