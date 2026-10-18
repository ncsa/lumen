"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-17

Creates daily_stats, probes and samples. Works on SQLite and PostgreSQL.

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('daily_stats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False, comment='Local date in the configured timezone'),
    sa.Column('model', sa.String(length=255), nullable=False, comment='Lumen model id'),
    sa.Column('source', sa.String(length=16), nullable=False, comment='models.dev, lumen or hardware'),
    sa.Column('last_updated', sa.DateTime(), nullable=False, comment='Last write (naive UTC)'),
    sa.Column('modelsdev_id', sa.String(length=255), nullable=True, comment='Matched models.dev model id'),
    sa.Column('input_median', sa.Float(), nullable=True, comment='Median provider input price, $/M tokens'),
    sa.Column('input_p5', sa.Float(), nullable=True, comment='5th percentile provider input price, $/M tokens'),
    sa.Column('input_p95', sa.Float(), nullable=True, comment='95th percentile provider input price, $/M tokens'),
    sa.Column('output_median', sa.Float(), nullable=True, comment='Median provider output price, $/M tokens'),
    sa.Column('output_p5', sa.Float(), nullable=True, comment='5th percentile provider output price, $/M tokens'),
    sa.Column('output_p95', sa.Float(), nullable=True, comment='95th percentile provider output price, $/M tokens'),
    sa.Column('providers', sa.Integer(), nullable=True, comment='Number of non-zero provider listings'),
    sa.Column('input_price', sa.Float(), nullable=True, comment='Lumen input price, $/M tokens'),
    sa.Column('output_price', sa.Float(), nullable=True, comment='Lumen output price, $/M tokens'),
    sa.Column('requests', sa.Float(), nullable=True, comment='Requests during the day (running total, may be prorated)'),
    sa.Column('tokens', sa.Float(), nullable=True, comment='Input + output tokens during the day (running total, may be prorated)'),
    sa.Column('covered_until', sa.DateTime(), nullable=True, comment='Requests/tokens are counted up to this time (naive UTC)'),
    sa.Column('estimated', sa.Boolean(), nullable=False, comment='Part of the totals was backfilled from a longer window'),
    sa.Column('final', sa.Boolean(), nullable=False, comment='The day has closed and the row no longer changes'),
    sa.Column('configured', sa.Integer(), nullable=True, comment='Configured instances at the last probe'),
    sa.Column('healthy', sa.Integer(), nullable=True, comment='Healthy instances at the last probe'),
    sa.Column('status', sa.String(length=16), nullable=True, comment='Model status at the last probe (ok, degraded, down)'),
    sa.Column('polls', sa.Integer(), nullable=False, comment='Probes that listed the model during the day'),
    sa.Column('polls_ok', sa.Integer(), nullable=False, comment='Probes where the model status was ok'),
    sa.Column('healthy_sum', sa.Integer(), nullable=False, comment='Sum of healthy instances over those probes'),
    sa.Column('daily_cost', sa.Float(), nullable=True, comment='Hardware + energy cost for the day, $'),
    sa.Column('gpu', sa.String(length=64), nullable=True, comment='GPU type from the models: config'),
    sa.Column('gpus', sa.Integer(), nullable=True, comment='GPUs per instance'),
    sa.Column('gpu_price', sa.Float(), nullable=True, comment='$ per GPU per month'),
    sa.Column('energy', sa.Float(), nullable=True, comment='Watts per GPU'),
    sa.Column('kwh', sa.Float(), nullable=True, comment='$ per kWh'),
    sa.Column('avg_healthy', sa.Float(), nullable=True, comment='Average healthy instances (healthy_sum / polls)'),
    sa.CheckConstraint("source IN ('models.dev', 'lumen', 'hardware')", name='ck_daily_stats_source'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('day', 'model', 'source', name='uq_daily_stats_day_model_source'),
    comment='One row per local day, Lumen model and price source; kept forever'
    )
    op.create_table('probes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('polled_at', sa.DateTime(), nullable=False, comment='Probe start (naive UTC)'),
    sa.Column('success', sa.Boolean(), nullable=False, comment='The probe got a valid response'),
    sa.Column('response_time_ms', sa.Float(), nullable=True, comment='Response time in milliseconds'),
    sa.Column('model_count', sa.Integer(), nullable=True, comment='Canonical models in the response'),
    sa.Column('models_up', sa.Integer(), nullable=True, comment='Canonical models whose status is not down'),
    sa.PrimaryKeyConstraint('id'),
    comment='One row per GET /v1/models probe; pruned after the retention period'
    )
    op.create_index('ix_probes_polled_at', 'probes', ['polled_at'])
    op.create_table('samples',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=255), nullable=False, comment='Lumen model id'),
    sa.Column('sampled_at', sa.DateTime(), nullable=False, comment='Probe start (naive UTC)'),
    sa.Column('status', sa.String(length=16), nullable=False, comment='ok, degraded or down'),
    sa.Column('configured', sa.Integer(), nullable=True, comment='Configured instances'),
    sa.Column('healthy', sa.Integer(), nullable=True, comment='Healthy instances'),
    sa.Column('requests_last_minute', sa.Integer(), nullable=True, comment='Requests in the minute before the probe'),
    sa.Column('tokens_last_minute', sa.Integer(), nullable=True, comment='Tokens in the minute before the probe, when Lumen reports them'),
    sa.PrimaryKeyConstraint('id'),
    comment='One row per canonical model per successful probe; pruned after the retention period'
    )
    op.create_index('ix_samples_model_sampled_at', 'samples', ['model', 'sampled_at'])


def downgrade():
    op.drop_index('ix_samples_model_sampled_at', table_name='samples')
    op.drop_table('samples')
    op.drop_index('ix_probes_polled_at', table_name='probes')
    op.drop_table('probes')
    op.drop_table('daily_stats')
