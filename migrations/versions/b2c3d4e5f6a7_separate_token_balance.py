"""separate token balance from model limits

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-19 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    # Create entity_model_balances table
    op.create_table(
        "entity_model_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("tokens_left", sa.BigInteger(), nullable=True, server_default="0"),
        sa.Column("last_refill_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "model_config_id", name="uq_emb_entity_model"),
    )

    # Seed balances from existing per-model model_limits rows
    op.execute(
        """
        INSERT INTO entity_model_balances (entity_id, model_config_id, tokens_left, last_refill_at)
        SELECT entity_id, model_config_id, tokens_left, last_refill_at
        FROM model_limits
        WHERE model_config_id IS NOT NULL
        """
    )

    # Drop state columns from model_limits
    with op.batch_alter_table("model_limits", schema=None) as batch_op:
        batch_op.drop_column("tokens_left")
        batch_op.drop_column("last_refill_at")


def downgrade():
    with op.batch_alter_table("model_limits", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_refill_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("tokens_left", sa.BigInteger(), nullable=False, server_default="0"))

    # Restore state from entity_model_balances where possible
    op.execute(
        """
        UPDATE model_limits
        SET tokens_left = emb.tokens_left,
            last_refill_at = emb.last_refill_at
        FROM entity_model_balances emb
        WHERE model_limits.entity_id = emb.entity_id
          AND model_limits.model_config_id = emb.model_config_id
        """
    )

    op.drop_table("entity_model_balances")
