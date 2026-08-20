"""Enforce at most one owner per project

Revision ID: ag7b8c9d0e1f
Revises: af6a7b8c9d0e
Create Date: 2026-08-20 00:10:00.000000

The groups feature gained a partial unique index against the concurrent
double-owner race (ab2c3d4e5f6a); projects had the identical race in
entity_managers all along. Any existing double-owner rows are healed first
(the lowest-id owner row wins), then the index makes the race impossible.

SQLite dev uses ``create_all`` + stamp head and never runs this chain, so
only the PostgreSQL path needs to be correct.
"""

import sqlalchemy as sa
from alembic import op

revision = "ag7b8c9d0e1f"
down_revision = "af6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        """
        UPDATE entity_managers SET is_owner = false
        WHERE is_owner AND id NOT IN (
            SELECT min(id) FROM entity_managers WHERE is_owner GROUP BY project_entity_id
        )
        """
    ))
    op.create_index(
        "uq_entity_managers_owner",
        "entity_managers",
        ["project_entity_id"],
        unique=True,
        postgresql_where=sa.text("is_owner"),
        sqlite_where=sa.text("is_owner"),
    )


def downgrade():
    op.drop_index("uq_entity_managers_owner", table_name="entity_managers")
