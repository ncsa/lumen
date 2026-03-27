"""Add TimescaleDB request_logs hypertable and continuous aggregate

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-03-26 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'i9j0k1l2m3n4'
down_revision = 'h8i9j0k1l2m3'
branch_labels = None
depends_on = None

_COLUMNS = [
    sa.Column('time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('model_config_id', sa.Integer(), nullable=True),
    sa.Column('model_endpoint_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(8), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('cost', sa.Numeric(12, 6), nullable=False, server_default='0'),
    sa.Column('duration', sa.Float(), nullable=False, server_default='0'),
]


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if _is_postgresql():
        # Create without a primary key so TimescaleDB can partition on 'time'
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
        op.execute("""
            CREATE TABLE request_logs (
                time            TIMESTAMPTZ NOT NULL,
                entity_id       INTEGER REFERENCES entities(id) ON DELETE SET NULL,
                model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL,
                model_endpoint_id INTEGER REFERENCES model_endpoints(id) ON DELETE SET NULL,
                source          VARCHAR(8) NOT NULL,
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                cost            NUMERIC(12,6) NOT NULL DEFAULT 0,
                duration        FLOAT NOT NULL DEFAULT 0
            )
        """)
        op.execute("SELECT create_hypertable('request_logs', 'time', chunk_time_interval => INTERVAL '7 days')")
        op.execute("""
            CREATE MATERIALIZED VIEW request_counts_hourly
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('1 hour', time) AS bucket,
                model_config_id,
                source,
                COUNT(*)          AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost)         AS cost
            FROM request_logs
            GROUP BY 1, 2, 3
            WITH NO DATA
        """)
        op.execute("""
            SELECT add_continuous_aggregate_policy('request_counts_hourly',
                start_offset => INTERVAL '3 hours',
                end_offset   => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 hour')
        """)
    else:
        # SQLite: plain table, no hypertable (analytics page requires PostgreSQL)
        op.create_table(
            'request_logs',
            sa.Column('time', sa.DateTime(timezone=True), nullable=False),
            *_COLUMNS[1:],
            sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['model_endpoint_id'], ['model_endpoints.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('time'),
        )


def downgrade():
    if _is_postgresql():
        op.execute("SELECT remove_continuous_aggregate_policy('request_counts_hourly')")
        op.execute("DROP MATERIALIZED VIEW IF EXISTS request_counts_hourly")
        op.execute("DROP TABLE IF EXISTS request_logs")
    else:
        op.drop_table('request_logs')
