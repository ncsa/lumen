"""Replace ix_messages_conversation_id with a (conversation_id, created_at) index

Revision ID: c93d4de8245c
Revises: a827b0223638
Create Date: 2026-10-17 00:00:00.000000

The chat sidebar's latest-message lookup (``max(created_at) GROUP BY
conversation_id``) and the ordered message fetch of a conversation both filter
on ``conversation_id`` and order or aggregate on ``created_at``; the composite
index serves both without a sort. Its leading column covers every lookup the
single-column index served, so that index is dropped.

On PostgreSQL both indexes are created and dropped ``CONCURRENTLY``, so chat
keeps writing to ``messages`` while the index builds during the upgrade.
``CONCURRENTLY`` cannot run inside a transaction, so the DDL runs in an
``autocommit_block``: it commits the open migration transaction first, which
means every revision applied before this one in the same run is committed even
if a later revision fails. The upgrade run is no longer all-or-nothing; running
it again resumes from where it stopped.

A concurrent build that fails or is interrupted leaves an INVALID index behind
while ``alembic_version`` still points at the previous revision. When migrating
online, an invalid leftover is dropped (concurrently) before the index is
created again; a valid one is kept, and ``IF NOT EXISTS`` skips rebuilding it.
Offline (``--sql``), the database cannot be inspected, so the script emits the
``CONCURRENTLY ... IF [NOT] EXISTS`` statements unguarded. Such a hand-applied
script cannot heal an invalid index; ``flask upgrade`` is the supported path.

SQLite and other dialects use plain ``CREATE INDEX`` / ``DROP INDEX``.
"""

from alembic import context, op
from sqlalchemy import text

revision = "c93d4de8245c"
down_revision = "a827b0223638"
branch_labels = None
depends_on = None

_COMPOSITE = "ix_messages_conversation_id_created_at"
_SINGLE = "ix_messages_conversation_id"


def _is_postgres():
    return op.get_bind().dialect.name == "postgresql"


def _drop_if_invalid(name):
    """Drop ``name`` concurrently if a failed concurrent build left it INVALID."""
    if context.is_offline_mode():
        return
    invalid = op.get_bind().execute(
        text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
        {"name": name},
    ).scalar()
    if invalid:
        op.drop_index(name, table_name="messages", postgresql_concurrently=True)


def upgrade():
    if _is_postgres():
        with op.get_context().autocommit_block():
            _drop_if_invalid(_COMPOSITE)
            op.create_index(
                _COMPOSITE, "messages", ["conversation_id", "created_at"],
                postgresql_concurrently=True, if_not_exists=True,
            )
            op.drop_index(_SINGLE, table_name="messages", postgresql_concurrently=True, if_exists=True)
        return

    op.create_index(_COMPOSITE, "messages", ["conversation_id", "created_at"])
    op.drop_index(_SINGLE, table_name="messages", if_exists=True)


def downgrade():
    if _is_postgres():
        with op.get_context().autocommit_block():
            _drop_if_invalid(_SINGLE)
            op.create_index(
                _SINGLE, "messages", ["conversation_id"],
                postgresql_concurrently=True, if_not_exists=True,
            )
            op.drop_index(_COMPOSITE, table_name="messages", postgresql_concurrently=True, if_exists=True)
        return

    op.create_index(_SINGLE, "messages", ["conversation_id"])
    op.drop_index(_COMPOSITE, table_name="messages")
