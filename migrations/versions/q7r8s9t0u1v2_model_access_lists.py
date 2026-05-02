"""Add whitelist/blacklist/graylist model access lists

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-05-02 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "q7r8s9t0u1v2"
down_revision = "p6q7r8s9t0u1"
branch_labels = None
depends_on = None


def upgrade():
    # Replace allowed:bool with access_type:str in group_model_access
    with op.batch_alter_table("group_model_access", schema=None) as batch_op:
        batch_op.add_column(sa.Column("access_type", sa.String(20), nullable=True))

    op.execute("UPDATE group_model_access SET access_type = 'whitelist' WHERE allowed = true")
    op.execute("UPDATE group_model_access SET access_type = 'blacklist' WHERE allowed = false")

    with op.batch_alter_table("group_model_access", schema=None) as batch_op:
        batch_op.alter_column("access_type", existing_type=sa.String(20), nullable=False)
        batch_op.drop_column("allowed")

    # Add model_access_default to groups
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("model_access_default", sa.String(20), nullable=True))

    # New global_model_access table
    op.create_table(
        "global_model_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("access_type", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_config_id"),
    )

    # New entity_model_consents table
    op.create_table(
        "entity_model_consents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("consented_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "model_config_id", name="uq_emc_entity_model"),
    )


def downgrade():
    op.drop_table("entity_model_consents")
    op.drop_table("global_model_access")

    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_column("model_access_default")

    with op.batch_alter_table("group_model_access", schema=None) as batch_op:
        batch_op.add_column(sa.Column("allowed", sa.Boolean(), nullable=True))

    op.execute("UPDATE group_model_access SET allowed = true WHERE access_type = 'whitelist'")
    op.execute("UPDATE group_model_access SET allowed = false WHERE access_type != 'whitelist'")

    with op.batch_alter_table("group_model_access", schema=None) as batch_op:
        batch_op.alter_column("allowed", existing_type=sa.Boolean(), nullable=False)
        batch_op.drop_column("access_type")
