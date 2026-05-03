"""Rename service_entity_id column and entity_type value to client

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-05-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "s9t0u1v2w3x4"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.alter_column("service_entity_id", new_column_name="client_entity_id")
    op.execute("UPDATE entities SET entity_type = 'client' WHERE entity_type = 'service'")


def downgrade():
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.alter_column("client_entity_id", new_column_name="service_entity_id")
    op.execute("UPDATE entities SET entity_type = 'service' WHERE entity_type = 'client'")
