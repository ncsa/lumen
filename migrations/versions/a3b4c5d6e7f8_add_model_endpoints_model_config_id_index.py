"""Add index on model_endpoints.model_config_id

Revision ID: a3b4c5d6e7f8
Revises: z0a1b2c3d4e5
Create Date: 2026-05-17 00:00:00.000000

"""

from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "z0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_model_endpoints_model_config_id", "model_endpoints", ["model_config_id"])


def downgrade():
    op.drop_index("ix_model_endpoints_model_config_id", table_name="model_endpoints")
