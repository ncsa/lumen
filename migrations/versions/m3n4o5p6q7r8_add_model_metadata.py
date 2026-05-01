"""Add context_window, max_output_tokens, supports_reasoning, knowledge_cutoff to model_configs

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-05-01 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('model_configs', sa.Column('context_window', sa.Integer(), nullable=True))
    op.add_column('model_configs', sa.Column('max_output_tokens', sa.Integer(), nullable=True))
    op.add_column('model_configs', sa.Column('supports_reasoning', sa.Boolean(), nullable=True))
    op.add_column('model_configs', sa.Column('knowledge_cutoff', sa.String(7), nullable=True))


def downgrade():
    op.drop_column('model_configs', 'knowledge_cutoff')
    op.drop_column('model_configs', 'supports_reasoning')
    op.drop_column('model_configs', 'max_output_tokens')
    op.drop_column('model_configs', 'context_window')
