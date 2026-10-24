"""split tokens

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24

Lumen reports input and output tokens separately for the monitoring token.
Daily rows get a total and a watermark for each, samples the last-minute
counts.

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.add_column(sa.Column('input_tokens', sa.Float(), nullable=True, comment='Input tokens during the day (running total, may be prorated)'))
        batch_op.add_column(sa.Column('output_tokens', sa.Float(), nullable=True, comment='Output tokens during the day (running total, may be prorated)'))
        batch_op.add_column(sa.Column('input_tokens_covered_until', sa.DateTime(), nullable=True, comment='Input tokens are counted up to this time (naive UTC)'))
        batch_op.add_column(sa.Column('output_tokens_covered_until', sa.DateTime(), nullable=True, comment='Output tokens are counted up to this time (naive UTC)'))
    with op.batch_alter_table('samples') as batch_op:
        batch_op.add_column(sa.Column('input_tokens_last_minute', sa.Integer(), nullable=True, comment='Input tokens in the minute before the probe, when Lumen reports them'))
        batch_op.add_column(sa.Column('output_tokens_last_minute', sa.Integer(), nullable=True, comment='Output tokens in the minute before the probe, when Lumen reports them'))


def downgrade():
    with op.batch_alter_table('samples') as batch_op:
        batch_op.drop_column('output_tokens_last_minute')
        batch_op.drop_column('input_tokens_last_minute')
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.drop_column('output_tokens_covered_until')
        batch_op.drop_column('input_tokens_covered_until')
        batch_op.drop_column('output_tokens')
        batch_op.drop_column('input_tokens')
