"""Add entity_stats table with pre-aggregated per-entity usage totals

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-05-08 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "w3x4y5z6a7b8"
down_revision = "v2w3x4y5z6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "entity_stats",
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entity_id"),
    )

    # Backfill from existing model_stats so the table is immediately usable
    op.execute("""
        INSERT INTO entity_stats (entity_id, requests, input_tokens, output_tokens, cost, last_used_at)
        SELECT entity_id,
               SUM(requests),
               SUM(input_tokens),
               SUM(output_tokens),
               SUM(cost),
               MAX(last_used_at)
        FROM model_stats
        GROUP BY entity_id
    """)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMENT ON TABLE entity_stats IS 'Aggregated usage totals per entity across all models and sources; updated on every proxied request'")
        op.execute("COMMENT ON COLUMN entity_stats.entity_id IS 'The entity these counters belong to'")
        op.execute("COMMENT ON COLUMN entity_stats.requests IS 'Total request count across all models and sources'")
        op.execute("COMMENT ON COLUMN entity_stats.input_tokens IS 'Total input tokens consumed across all models and sources'")
        op.execute("COMMENT ON COLUMN entity_stats.output_tokens IS 'Total output tokens produced across all models and sources'")
        op.execute("COMMENT ON COLUMN entity_stats.cost IS 'Total cost in USD across all models and sources'")
        op.execute("COMMENT ON COLUMN entity_stats.last_used_at IS 'UTC timestamp of the most recent request by this entity'")


def downgrade():
    op.drop_table("entity_stats")
