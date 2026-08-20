"""Move group auto-join rules into the database

Revision ID: ac3d4e5f6a7b
Revises: ab2c3d4e5f6a
Create Date: 2026-08-19 00:00:00.000000

Group auto-join rules move from config.yaml's group_rules section into a new
group_rules table, edited on the group detail page. groups.auto_join replaces
groups.config_managed: the old flag meant "auto-created because a config.yaml
rule names this group", which is carried over as auto_join=true; the rules
themselves are imported from config.yaml at the next startup (deprecated
one-time import in lumen.commands.import_group_rules_from_yaml).

SQLite dev uses ``create_all`` + stamp head and never runs this chain, so
only the PostgreSQL path needs to be correct.
"""

import sqlalchemy as sa
from alembic import op

revision = "ac3d4e5f6a7b"
down_revision = "ab2c3d4e5f6a"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    op.create_table(
        "group_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.String(128), nullable=False),
        sa.Column("match", sa.String(8), nullable=False),
        sa.Column("value", sa.String(256), nullable=False),
    )
    op.create_index("ix_group_rules_group_id", "group_rules", ["group_id"])

    with op.batch_alter_table("groups") as batch_op:
        batch_op.add_column(
            sa.Column("auto_join", sa.Boolean(), nullable=False, server_default=sa.text("false"))
        )
    # Groups auto-created from config.yaml rules keep auto-joining; their rules
    # are imported from config.yaml at the next startup.
    op.execute("UPDATE groups SET auto_join = config_managed")
    with op.batch_alter_table("groups") as batch_op:
        batch_op.drop_column("config_managed")

    if _is_postgresql():
        op.execute(
            f"COMMENT ON TABLE group_rules IS "
            f"{_q('Auto-join conditions per group; a user matching ALL of a group rules is added at login')}"
        )
        op.execute(f"COMMENT ON COLUMN group_rules.id IS {_q('Primary key')}")
        op.execute(f"COMMENT ON COLUMN group_rules.group_id IS {_q('The group this rule belongs to')}")
        op.execute(f"COMMENT ON COLUMN group_rules.field IS {_q('Userinfo claim to inspect (e.g. affiliation, idp)')}")
        op.execute(f"COMMENT ON COLUMN group_rules.match IS {_q('contains for substring match, equals for exact match')}")
        op.execute(f"COMMENT ON COLUMN group_rules.value IS {_q('Value the claim is compared against')}")
        op.execute(
            f"COMMENT ON COLUMN groups.auto_join IS "
            f"{_q('When true, users matching all of the group rules are added as members at OAuth login')}"
        )
        op.execute(
            f"COMMENT ON COLUMN group_members.config_managed IS "
            f"{_q('When true, assigned automatically at login by the group auto-join rules; reconciled at each login')}"
        )


def downgrade():
    with op.batch_alter_table("groups") as batch_op:
        batch_op.add_column(
            sa.Column("config_managed", sa.Boolean(), nullable=False, server_default=sa.text("false"))
        )
    op.execute("UPDATE groups SET config_managed = auto_join")
    with op.batch_alter_table("groups") as batch_op:
        batch_op.drop_column("auto_join")
    op.drop_index("ix_group_rules_group_id", table_name="group_rules")
    op.drop_table("group_rules")
