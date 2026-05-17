"""Enforce NOT NULL on entity_balances columns and timezone-aware last_refill_at; NOT NULL on api_keys.key_hash

Revision ID: a1b2c3d4e5f6
Revises: z0a1b2c3d4e5
Create Date: 2026-05-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "z0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    # Backfill any NULLs before adding NOT NULL constraints (defensive; should not exist)
    op.execute("UPDATE entity_balances SET coins_left = 0 WHERE coins_left IS NULL")
    op.execute("UPDATE entity_balances SET last_refill_at = NOW() WHERE last_refill_at IS NULL")

    with op.batch_alter_table("entity_balances") as batch_op:
        batch_op.alter_column(
            "coins_left",
            existing_type=sa.Numeric(12, 6),
            nullable=False,
        )
        batch_op.alter_column(
            "last_refill_at",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            nullable=False,
            postgresql_using="last_refill_at AT TIME ZONE 'UTC'",
        )

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column(
            "key_hash",
            existing_type=sa.String(64),
            nullable=False,
        )


def downgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column(
            "key_hash",
            existing_type=sa.String(64),
            nullable=True,
        )

    with op.batch_alter_table("entity_balances") as batch_op:
        batch_op.alter_column(
            "last_refill_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            nullable=True,
            postgresql_using="last_refill_at AT TIME ZONE 'UTC'",
        )
        batch_op.alter_column(
            "coins_left",
            existing_type=sa.Numeric(12, 6),
            nullable=True,
        )
