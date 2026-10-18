"""Replace ix_messages_conversation_id with a (conversation_id, created_at) index

Revision ID: c93d4de8245c
Revises: a827b0223638
Create Date: 2026-10-17 00:00:00.000000

The chat sidebar's latest-message lookup (``max(created_at) GROUP BY
conversation_id``) and the ordered message fetch of a conversation both filter
on ``conversation_id`` and order or aggregate on ``created_at``; the composite
index serves both without a sort. Its leading column covers every lookup the
single-column index served, so that index is dropped. A plain ``CREATE INDEX``
(not ``CONCURRENTLY``) locks writes to ``messages`` while it builds.
"""

from alembic import op

revision = "c93d4de8245c"
down_revision = "a827b0223638"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_messages_conversation_id_created_at",
        "messages",
        ["conversation_id", "created_at"],
    )
    op.drop_index("ix_messages_conversation_id", table_name="messages", if_exists=True)


def downgrade():
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.drop_index("ix_messages_conversation_id_created_at", table_name="messages")
