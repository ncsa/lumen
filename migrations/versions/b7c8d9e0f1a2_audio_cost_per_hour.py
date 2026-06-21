"""rename model_configs.audio_cost_per_minute -> audio_cost_per_hour (value ×60)

Audio pricing is now expressed per hour (e.g. $0.10/hour) rather than per minute,
which kept many leading zeros for cheap ASR models. Existing per-minute values are
multiplied by 60 to preserve the effective rate.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-06-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_cost_per_hour", sa.Numeric(12, 6), nullable=True,
                                      comment="USD cost per hour of audio; only set for speech-to-text models"))
    op.execute("UPDATE model_configs SET audio_cost_per_hour = audio_cost_per_minute * 60 WHERE audio_cost_per_minute IS NOT NULL")
    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.drop_column("audio_cost_per_minute")


def downgrade():
    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_cost_per_minute", sa.Numeric(12, 6), nullable=True,
                                      comment="USD cost per minute of audio; only set for speech-to-text models"))
    op.execute("UPDATE model_configs SET audio_cost_per_minute = audio_cost_per_hour / 60 WHERE audio_cost_per_hour IS NOT NULL")
    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.drop_column("audio_cost_per_hour")
