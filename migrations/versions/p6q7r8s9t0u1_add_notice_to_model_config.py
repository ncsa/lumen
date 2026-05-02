"""Add notice column to model_configs

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-05-01 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = 'p6q7r8s9t0u1'
down_revision = 'o5p6q7r8s9t0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('model_configs', sa.Column('notice', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('model_configs', 'notice')
