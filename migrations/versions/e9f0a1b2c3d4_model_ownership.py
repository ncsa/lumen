"""Replace allow/block access lists with model ownership and group grants

Models get an optional owner (a user entity). A model without an owner is
available to everyone; an owned model is available only to its owner and to
members of groups it has been granted to (model_group_access).

Drops entity_model_access, group_model_access, model_configs.access,
entities.model_access_default and groups.model_access_default. Downgrade
recreates the dropped structures empty — the old access rules are NOT
restored.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-15 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_group_access",
        sa.Column("id", sa.Integer(), primary_key=True, comment="Primary key"),
        sa.Column("model_config_id", sa.Integer(), sa.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False,
                  comment="The owned model being granted"),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
                  comment="The group whose members are granted access"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="UTC timestamp when the grant was created"),
        sa.UniqueConstraint("model_config_id", "group_id", name="uq_mga_model_group"),
        comment="Group grants for owned models; a row gives all group members access to the model",
    )
    op.create_index("ix_model_group_access_group_id", "model_group_access", ["group_id"])

    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.add_column(sa.Column("owner_entity_id", sa.Integer(), nullable=True,
                            comment="Owning user entity; NULL = available to everyone. When set, only the owner and members of granted groups may use the model"))
        batch_op.create_foreign_key("fk_model_configs_owner_entity_id", "entities", ["owner_entity_id"], ["id"], ondelete="SET NULL")
        batch_op.drop_column("access")

    op.drop_table("entity_model_access")
    op.drop_table("group_model_access")
    with op.batch_alter_table("entities") as batch_op:
        batch_op.drop_column("model_access_default")
    with op.batch_alter_table("groups") as batch_op:
        batch_op.drop_column("model_access_default")


def downgrade():
    with op.batch_alter_table("groups") as batch_op:
        batch_op.add_column(sa.Column("model_access_default", sa.String(20), nullable=True,
                            comment="Default model access policy for this group: 'allowed' or 'blocked'"))
    with op.batch_alter_table("entities") as batch_op:
        batch_op.add_column(sa.Column("model_access_default", sa.String(16), nullable=True,
                            comment="Default model access policy: 'allowed' or 'blocked'; project entities only"))
    op.create_table(
        "group_model_access",
        sa.Column("id", sa.Integer(), primary_key=True, comment="Primary key"),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
                  comment="The group the override applies to"),
        sa.Column("model_config_id", sa.Integer(), sa.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False,
                  comment="The model being overridden"),
        sa.Column("access_type", sa.String(20), nullable=False,
                  comment="'allowed' or 'blocked' for this group; acknowledgement requirement lives on the model"),
        sa.UniqueConstraint("group_id", "model_config_id", name="uq_gma_group_model"),
        comment="Per-group model access overrides; lower priority than entity_model_access rows",
    )
    op.create_index("ix_group_model_access_group_id", "group_model_access", ["group_id"])
    op.create_table(
        "entity_model_access",
        sa.Column("id", sa.Integer(), primary_key=True, comment="Primary key"),
        sa.Column("entity_id", sa.Integer(), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
                  comment="The entity the override applies to"),
        sa.Column("model_config_id", sa.Integer(), sa.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False,
                  comment="The model being overridden"),
        sa.Column("access_type", sa.String(20), nullable=False,
                  comment="'allowed' or 'blocked' for this entity; acknowledgement requirement lives on the model"),
        sa.UniqueConstraint("entity_id", "model_config_id", name="uq_ema_entity_model"),
        comment="Per-entity model access overrides; entity-level takes precedence over group-level",
    )
    op.create_index("ix_entity_model_access_entity_id", "entity_model_access", ["entity_id"])
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.add_column(sa.Column("access", sa.String(8), nullable=True,
                            comment="Per-model default access: 'allowed', 'blocked', or NULL to inherit group/global defaults; overridden only by explicit per-scope rules"))
        batch_op.drop_constraint("fk_model_configs_owner_entity_id", type_="foreignkey")
        batch_op.drop_column("owner_entity_id")
    op.drop_index("ix_model_group_access_group_id", table_name="model_group_access")
    op.drop_table("model_group_access")
