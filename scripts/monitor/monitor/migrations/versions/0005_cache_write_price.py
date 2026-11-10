"""cache write price

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07

Daily lumen rows gain the cache-write price, when Lumen charges one. It backs
the "cache write" line of the Our $/M column; models without a write price
keep a NULL.

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.add_column(sa.Column('cache_write_price', sa.Float(), nullable=True, comment='Lumen cache-write price, $/M tokens'))


def downgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.drop_column('cache_write_price')
