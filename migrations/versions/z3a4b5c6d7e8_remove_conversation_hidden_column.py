"""Remove conversations.hidden column; always hard-delete conversations

Revision ID: z3a4b5c6d7e8
Revises: z2a3b4c5d6e7
Create Date: 2026-06-05 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "z3a4b5c6d7e8"
down_revision = "z2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    # Remove any soft-deleted conversations (their messages cascade-delete automatically)
    op.execute("DELETE FROM conversations WHERE hidden = TRUE")

    op.drop_index("ix_conversations_entity_hidden_updated", table_name="conversations")
    op.drop_column("conversations", "hidden")
    op.create_index("ix_conversations_entity_updated", "conversations", ["entity_id", "updated_at"])


def downgrade():
    op.drop_index("ix_conversations_entity_updated", table_name="conversations")
    op.add_column("conversations", sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index(
        "ix_conversations_entity_hidden_updated", "conversations", ["entity_id", "hidden", "updated_at"]
    )
