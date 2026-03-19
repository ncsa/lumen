"""groups and reworked token limits

Revision ID: a1b2c3d4e5f6
Revises: f3a8b2c1d0e9
Create Date: 2026-03-18 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f3a8b2c1d0e9"
branch_labels = None
depends_on = None


def upgrade():
    # --- model_limits: rename columns, add starting_tokens, make model_config_id nullable ---
    # The old unnamed UniqueConstraint(entity_id, model_config_id) is kept; it's harmless
    # since NULL != NULL in SQL, and the partial indexes below enforce the new rules.
    with op.batch_alter_table("model_limits", schema=None) as batch_op:
        batch_op.alter_column("token_limit", new_column_name="max_tokens")
        batch_op.alter_column("tokens_per_hour", new_column_name="refresh_tokens")
        batch_op.add_column(sa.Column("starting_tokens", sa.BigInteger(), nullable=False, server_default="0"))
        batch_op.alter_column("model_config_id", existing_type=sa.Integer(), nullable=True)

    op.execute(
        "CREATE UNIQUE INDEX uq_ml_entity_model ON model_limits (entity_id, model_config_id) "
        "WHERE model_config_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ml_entity_default ON model_limits (entity_id) "
        "WHERE model_config_id IS NULL"
    )

    # --- groups table ---
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # --- group_members table ---
    op.create_table(
        "group_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "entity_id"),
    )

    # --- group_model_limits table ---
    op.create_table(
        "group_model_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=True),
        sa.Column("max_tokens", sa.BigInteger(), nullable=False, server_default="-1"),
        sa.Column("refresh_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starting_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        "CREATE UNIQUE INDEX uq_gml_group_model ON group_model_limits (group_id, model_config_id) "
        "WHERE model_config_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_gml_group_default ON group_model_limits (group_id) "
        "WHERE model_config_id IS NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_gml_group_default")
    op.execute("DROP INDEX IF EXISTS uq_gml_group_model")
    op.drop_table("group_model_limits")
    op.drop_table("group_members")
    op.drop_table("groups")

    op.execute("DROP INDEX IF EXISTS uq_ml_entity_default")
    op.execute("DROP INDEX IF EXISTS uq_ml_entity_model")

    with op.batch_alter_table("model_limits", schema=None) as batch_op:
        batch_op.alter_column("model_config_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("starting_tokens")
        batch_op.alter_column("refresh_tokens", new_column_name="tokens_per_hour")
        batch_op.alter_column("max_tokens", new_column_name="token_limit")
