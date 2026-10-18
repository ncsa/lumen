"""tokens covered_until

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

Requests and tokens are counted independently, so each gets its own
watermark: covered_until is now for requests, tokens_covered_until for tokens.

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.add_column(sa.Column('tokens_covered_until', sa.DateTime(), nullable=True, comment='Tokens are counted up to this time (naive UTC)'))
        batch_op.alter_column('covered_until', existing_type=sa.DateTime(), existing_nullable=True,
                              comment='Requests are counted up to this time (naive UTC)',
                              existing_comment='Requests/tokens are counted up to this time (naive UTC)')


def downgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.alter_column('covered_until', existing_type=sa.DateTime(), existing_nullable=True,
                              comment='Requests/tokens are counted up to this time (naive UTC)',
                              existing_comment='Requests are counted up to this time (naive UTC)')
        batch_op.drop_column('tokens_covered_until')
