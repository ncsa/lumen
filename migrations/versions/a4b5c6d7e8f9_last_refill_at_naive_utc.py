"""Revert last_refill_at to naive UTC (TIMESTAMP WITHOUT TIME ZONE)

All other DateTime columns in the schema use naive UTC. Aligning
last_refill_at removes the tz-aware/tz-naive comparison hazard in
token_refill.py and admin routes.

Revision ID: a4b5c6d7e8f9
Revises: z2a3b4c5d6e7
Create Date: 2026-05-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a4b5c6d7e8f9"
down_revision = "z2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("entity_balances") as batch_op:
        batch_op.alter_column(
            "last_refill_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            nullable=False,
            postgresql_using="last_refill_at AT TIME ZONE 'UTC'",
        )


def downgrade():
    with op.batch_alter_table("entity_balances") as batch_op:
        batch_op.alter_column(
            "last_refill_at",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            nullable=False,
            postgresql_using="last_refill_at AT TIME ZONE 'UTC'",
        )
