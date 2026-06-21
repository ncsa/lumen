"""orthogonal model access (access/needs_ack/disabled), remap scope access_type

Replaces ModelConfig.active with access/needs_ack/ack_message/disabled and
remaps the whitelist/blacklist/graylist vocabulary in the scope-access tables
and defaults to the new allowed/blocked vocabulary.

Revision ID: a6b7c8d9e0f1
Revises: a5b6c7d8e9f0
Create Date: 2026-06-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "a5b6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade():
    # --- model_configs: add new columns, backfill from active, drop active ---
    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("access", sa.String(length=8), nullable=True,
                                      comment="Per-model default access: 'allowed', 'blocked', or NULL to inherit group/global defaults; overridden only by explicit per-scope rules"))
        batch_op.add_column(sa.Column("needs_ack", sa.Boolean(), nullable=False, server_default=sa.false(),
                                      comment="Requires user acknowledgement before use; sticky model-level property, not overridable by scopes"))
        batch_op.add_column(sa.Column("ack_message", sa.Text(), nullable=True,
                                      comment="Per-model acknowledgement message; overrides the global defaults.models.ack_message"))
        batch_op.add_column(sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false(),
                                      comment="Hard off: hidden everywhere and not overridable by any scope"))

    # Preserve current behavior: previously-inactive models become disabled. Active models
    # keep access=NULL (inherit group/global defaults), matching the pre-refactor group-driven model.
    op.execute("UPDATE model_configs SET disabled=true WHERE active=false")

    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.drop_column("active")

    # Backfill needs_ack from prior per-model 'graylist' rules BEFORE remapping them away.
    # Acknowledgement is now a model property; a model graylisted by any scope rule should
    # keep requiring consent. (A scope *default* of 'graylist' has no model list and cannot
    # be reconstructed — those need a manual needs_ack review; see CHANGELOG upgrade note.)
    op.execute("""
        UPDATE model_configs SET needs_ack=true WHERE id IN (
            SELECT model_config_id FROM entity_model_access WHERE access_type='graylist'
            UNION
            SELECT model_config_id FROM group_model_access WHERE access_type='graylist'
        )
    """)

    # --- remap scope access vocabulary: whitelist/graylist -> allowed, blacklist -> blocked ---
    for table in ("entity_model_access", "group_model_access"):
        op.execute(f"UPDATE {table} SET access_type='allowed' WHERE access_type IN ('whitelist', 'graylist')")
        op.execute(f"UPDATE {table} SET access_type='blocked' WHERE access_type='blacklist'")

    for table in ("entities", "groups"):
        op.execute(f"UPDATE {table} SET model_access_default='allowed' WHERE model_access_default IN ('whitelist', 'graylist')")
        op.execute(f"UPDATE {table} SET model_access_default='blocked' WHERE model_access_default='blacklist'")


def downgrade():
    # Reverse the vocabulary remap (graylist is not reconstructable; allowed -> whitelist).
    for table in ("entity_model_access", "group_model_access"):
        op.execute(f"UPDATE {table} SET access_type='whitelist' WHERE access_type='allowed'")
        op.execute(f"UPDATE {table} SET access_type='blacklist' WHERE access_type='blocked'")

    for table in ("entities", "groups"):
        op.execute(f"UPDATE {table} SET model_access_default='whitelist' WHERE model_access_default='allowed'")
        op.execute(f"UPDATE {table} SET model_access_default='blacklist' WHERE model_access_default='blocked'")

    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true"),
                                      comment="Inactive models are hidden and cannot be used"))

    op.execute("UPDATE model_configs SET active=false WHERE disabled=true")

    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.drop_column("disabled")
        batch_op.drop_column("ack_message")
        batch_op.drop_column("needs_ack")
        batch_op.drop_column("access")
