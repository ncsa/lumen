"""Add model capabilities columns

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-04-05 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('model_configs', sa.Column('max_input_tokens', sa.Integer(), nullable=True))
    op.add_column('model_configs', sa.Column('supports_function_calling', sa.Boolean(), nullable=True))
    op.add_column('model_configs', sa.Column('supports_vision', sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column('model_configs', 'supports_vision')
    op.drop_column('model_configs', 'supports_function_calling')
    op.drop_column('model_configs', 'max_input_tokens')
