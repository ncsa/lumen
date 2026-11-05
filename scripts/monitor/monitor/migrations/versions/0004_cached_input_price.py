"""cached input price

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05

Daily lumen rows gain the cached-input price, when Lumen charges one. It
backs the "cached in" line of the Our $/M column; models without a cached
price keep a NULL.

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.add_column(sa.Column('cached_input_price', sa.Float(), nullable=True, comment='Lumen cached-input price, $/M tokens'))


def downgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.drop_column('cached_input_price')
