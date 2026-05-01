"""Replace supports_vision with input_modalities JSON array

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-05-01 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = 'n4o5p6q7r8s9'
down_revision = 'm3n4o5p6q7r8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('model_configs', sa.Column('input_modalities', sa.JSON(), nullable=True))
    op.execute("""
        UPDATE model_configs
        SET input_modalities = CASE
            WHEN supports_vision = true THEN '["text", "image"]'::json
            WHEN supports_vision = false THEN '["text"]'::json
            ELSE NULL
        END
        WHERE supports_vision IS NOT NULL
    """)
    op.drop_column('model_configs', 'supports_vision')


def downgrade():
    op.add_column('model_configs', sa.Column('supports_vision', sa.Boolean(), nullable=True))
    op.execute("""
        UPDATE model_configs
        SET supports_vision = (
            input_modalities::text LIKE '%"image"%'
        )
        WHERE input_modalities IS NOT NULL
    """)
    op.drop_column('model_configs', 'input_modalities')
