"""Add description and url to model_configs

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-03-26 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'j0k1l2m3n4o5'
down_revision = 'i9j0k1l2m3n4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('model_configs', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('model_configs', sa.Column('url', sa.String(512), nullable=True))


def downgrade():
    op.drop_column('model_configs', 'url')
    op.drop_column('model_configs', 'description')
