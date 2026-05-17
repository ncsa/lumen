"""Add index on entity_managers.client_entity_id

Revision ID: a1b2c3d4e5f7
Revises: z0a1b2c3d4e5
Create Date: 2026-05-17 00:00:00.000000

"""

from alembic import op

revision = "a1b2c3d4e5f7"
down_revision = "z0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_entity_managers_client_entity_id",
        "entity_managers",
        ["client_entity_id"],
    )


def downgrade():
    op.drop_index("ix_entity_managers_client_entity_id", table_name="entity_managers")
