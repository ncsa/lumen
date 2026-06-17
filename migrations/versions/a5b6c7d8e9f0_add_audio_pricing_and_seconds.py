"""Add audio per-minute pricing and audio_seconds usage tracking

Revision ID: a5b6c7d8e9f0
Revises: z4b5c6d7e8f9
Create Date: 2026-06-15 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a5b6c7d8e9f0"
down_revision = "z4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_cost_per_minute", sa.Numeric(12, 6), nullable=True))

    with op.batch_alter_table("request_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_seconds", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("model_stats", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_seconds", sa.BigInteger(), nullable=False, server_default="0"))

    with op.batch_alter_table("entity_stats", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_seconds", sa.BigInteger(), nullable=False, server_default="0"))

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_seconds", sa.BigInteger(), nullable=False, server_default="0"))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMENT ON COLUMN model_configs.audio_cost_per_minute IS 'USD cost per minute of audio; only set for speech-to-text models'")
        op.execute("COMMENT ON COLUMN request_logs.audio_seconds IS 'Seconds of audio transcribed/translated; 0 for text requests'")
        op.execute("COMMENT ON COLUMN model_stats.audio_seconds IS 'Total seconds of audio transcribed/translated'")
        op.execute("COMMENT ON COLUMN entity_stats.audio_seconds IS 'Total seconds of audio transcribed/translated across all models and sources'")
        op.execute("COMMENT ON COLUMN api_keys.audio_seconds IS 'Cumulative seconds of audio transcribed/translated via this key'")


def downgrade():
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_column("audio_seconds")

    with op.batch_alter_table("entity_stats", schema=None) as batch_op:
        batch_op.drop_column("audio_seconds")

    with op.batch_alter_table("model_stats", schema=None) as batch_op:
        batch_op.drop_column("audio_seconds")

    with op.batch_alter_table("request_logs", schema=None) as batch_op:
        batch_op.drop_column("audio_seconds")

    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.drop_column("audio_cost_per_minute")
