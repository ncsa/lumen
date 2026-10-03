"""Add created_by_entity_id column to api_keys

Revision ID: d6e7f8a9b0c1
Revises: q1w2e3r4t5y6
Create Date: 2026-10-03 00:00:00.000000

Adds a nullable ``created_by_entity_id`` FK to ``api_keys`` recording which
user created each key. ``ON DELETE SET NULL`` keeps the keys of a deleted
creator in place, and legacy keys stay NULL — there is no reliable backfill.
batch_alter_table keeps the statement portable; SQLite dev uses
``create_all`` + stamp head and never runs this chain (matching aa1b2c3d4e5f).
"""

import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "q1w2e3r4t5y6"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(
            sa.Column(
                "created_by_entity_id",
                sa.Integer(),
                # Explicit name = PostgreSQL's implicit naming, so migrated and
                # create_all databases match, and SQLite batch recreate has a name.
                sa.ForeignKey(
                    "entities.id",
                    ondelete="SET NULL",
                    name="api_keys_created_by_entity_id_fkey",
                ),
                nullable=True,
            )
        )

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN api_keys.created_by_entity_id IS "
            f"{_q('Entity (user) that created this key; null for legacy keys or if the creator was deleted')}"
        )


def downgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("created_by_entity_id")
