"""Replace allowed:bool with access_type:str in entity_model_access

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-05-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "t0u1v2w3x4y5"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("entity_model_access", schema=None) as batch_op:
        batch_op.add_column(sa.Column("access_type", sa.String(20), nullable=True))

    op.execute("UPDATE entity_model_access SET access_type = 'whitelist' WHERE allowed = true")
    op.execute("UPDATE entity_model_access SET access_type = 'blacklist' WHERE allowed = false")

    with op.batch_alter_table("entity_model_access", schema=None) as batch_op:
        batch_op.alter_column("access_type", existing_type=sa.String(20), nullable=False)
        batch_op.drop_column("allowed")


def downgrade():
    with op.batch_alter_table("entity_model_access", schema=None) as batch_op:
        batch_op.add_column(sa.Column("allowed", sa.Boolean(), nullable=True))

    op.execute("UPDATE entity_model_access SET allowed = true WHERE access_type = 'whitelist'")
    op.execute("UPDATE entity_model_access SET allowed = false WHERE access_type != 'whitelist'")

    with op.batch_alter_table("entity_model_access", schema=None) as batch_op:
        batch_op.alter_column("allowed", existing_type=sa.Boolean(), nullable=False)
        batch_op.drop_column("access_type")
