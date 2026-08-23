"""Add early_access and end_date to model_configs; track per-requirement consent

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-15 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_configs", sa.Column("early_access", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="Early-access model: requires acknowledgement that it may change or be removed; sticky model-level property"))
    op.add_column("model_configs", sa.Column("end_date", sa.DateTime(), nullable=True,
                  comment="Naive-UTC datetime after which the model is hidden and rejected (exclusive); NULL = no end date"))
    with op.batch_alter_table("entity_model_consents") as batch_op:
        batch_op.alter_column("consented_at", existing_type=sa.DateTime(), nullable=True,
                              comment="UTC timestamp when the entity acknowledged the model notice (needs_ack); NULL if never required")
        batch_op.add_column(sa.Column("early_access_at", sa.DateTime(), nullable=True,
                            comment="UTC timestamp when the entity acknowledged the early-access warning; NULL if never required"))


def downgrade():
    # Keep the NOT NULL restore valid: fall back to the early-access timestamp
    # for rows that only acknowledged early access.
    op.execute("UPDATE entity_model_consents SET consented_at = early_access_at WHERE consented_at IS NULL")
    with op.batch_alter_table("entity_model_consents") as batch_op:
        batch_op.drop_column("early_access_at")
        batch_op.alter_column("consented_at", existing_type=sa.DateTime(), nullable=False,
                              comment="UTC timestamp when the entity accepted the model notice")
    op.drop_column("model_configs", "end_date")
    op.drop_column("model_configs", "early_access")
