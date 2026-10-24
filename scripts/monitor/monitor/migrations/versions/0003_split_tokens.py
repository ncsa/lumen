"""split tokens

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24

Lumen reports input and output tokens separately for the monitoring token.
Daily rows get a total and a watermark for each, samples the last-minute
counts. The combined token columns go: the total is the sum of the two, and
Lumen never filled them before the split.

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
        batch_op.drop_column('tokens_covered_until')
        batch_op.drop_column('tokens')
    with op.batch_alter_table('samples') as batch_op:
        batch_op.add_column(sa.Column('input_tokens_last_minute', sa.Integer(), nullable=True, comment='Input tokens in the minute before the probe, when Lumen reports them'))
        batch_op.add_column(sa.Column('output_tokens_last_minute', sa.Integer(), nullable=True, comment='Output tokens in the minute before the probe, when Lumen reports them'))
        batch_op.drop_column('tokens_last_minute')


def downgrade():
    # The combined columns come back empty; the split counts are not summed into them.
    with op.batch_alter_table('samples') as batch_op:
        batch_op.add_column(sa.Column('tokens_last_minute', sa.Integer(), nullable=True, comment='Tokens in the minute before the probe, when Lumen reports them'))
        batch_op.drop_column('output_tokens_last_minute')
        batch_op.drop_column('input_tokens_last_minute')
    with op.batch_alter_table('daily_stats') as batch_op:
        batch_op.add_column(sa.Column('tokens', sa.Float(), nullable=True, comment='Input + output tokens during the day (running total, may be prorated)'))
        batch_op.add_column(sa.Column('tokens_covered_until', sa.DateTime(), nullable=True, comment='Tokens are counted up to this time (naive UTC)'))
        batch_op.drop_column('output_tokens_covered_until')
        batch_op.drop_column('input_tokens_covered_until')
        batch_op.drop_column('output_tokens')
        batch_op.drop_column('input_tokens')
