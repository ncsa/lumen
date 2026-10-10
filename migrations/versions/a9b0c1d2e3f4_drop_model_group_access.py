"""Drop model_group_access

Owned models are usable only by their owner, so group grants are no longer
read anywhere. Drops the ``model_group_access`` table and its index, and
updates the ``model_configs.owner_entity_id`` and ``groups`` comments that
mentioned grants. Downgrade recreates the table and index empty — the old
grants are NOT restored.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-10-08 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

OWNER_COMMENT = "Owning user entity; NULL = available to everyone. When set, only the owner may use the model"
OLD_OWNER_COMMENT = "Owning user entity; NULL = available to everyone. When set, only the owner and members of granted groups may use the model"
GROUPS_COMMENT = "Named collections of entities for coin limit policy assignment"
OLD_GROUPS_COMMENT = "Named collections of entities for bulk model access and coin limit policy assignment"


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def _set_comments(owner_comment, groups_comment):
    if _is_postgresql():
        op.execute(f"COMMENT ON COLUMN model_configs.owner_entity_id IS {_q(owner_comment)}")
        op.execute(f"COMMENT ON TABLE groups IS {_q(groups_comment)}")


def upgrade():
    op.drop_index("ix_model_group_access_group_id", table_name="model_group_access")
    op.drop_table("model_group_access")
    _set_comments(OWNER_COMMENT, GROUPS_COMMENT)


def downgrade():
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
    _set_comments(OLD_OWNER_COMMENT, OLD_GROUPS_COMMENT)
