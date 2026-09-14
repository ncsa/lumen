"""Add model_aliases table for backward-compatible model upgrades

Revision ID: c9d8e7f6a5b4
Revises: b0c1d2e3f4a5
Create Date: 2026-09-14 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c9d8e7f6a5b4"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("alias"),
    )
    op.create_index("ix_model_aliases_model_config_id", "model_aliases", ["model_config_id"])
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMENT ON TABLE model_aliases IS 'Backward-compatible alias names that resolve to a canonical model_configs row'")
        op.execute("COMMENT ON COLUMN model_aliases.id IS 'Primary key'")
        op.execute("COMMENT ON COLUMN model_aliases.alias IS 'Alias name clients may request; unique and never equal to any canonical model name'")
        op.execute("COMMENT ON COLUMN model_aliases.model_config_id IS 'Canonical model_config this alias resolves to; CASCADE delete'")
        op.execute("COMMENT ON COLUMN model_aliases.created_at IS 'UTC timestamp when the alias was created'")


def downgrade():
    op.drop_index("ix_model_aliases_model_config_id", table_name="model_aliases")
    op.drop_table("model_aliases")
