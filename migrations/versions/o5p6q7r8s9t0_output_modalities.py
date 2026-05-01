"""Add output_modalities column to model_configs

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-05-01 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = 'o5p6q7r8s9t0'
down_revision = 'n4o5p6q7r8s9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('model_configs', sa.Column('output_modalities', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('model_configs', 'output_modalities')
