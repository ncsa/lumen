"""Drop deprecated max_input_tokens column; add entity_type CHECK constraint

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-17 00:00:01.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6g7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.drop_column("max_input_tokens")

    op.execute("""
        ALTER TABLE entities ADD CONSTRAINT ck_entities_type
        CHECK (entity_type IN ('user', 'client'))
    """)


def downgrade():
    op.execute("ALTER TABLE entities DROP CONSTRAINT ck_entities_type")

    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.add_column(
            sa.Column("max_input_tokens", sa.Integer(), nullable=True, comment="Deprecated; use context_window instead")
        )
