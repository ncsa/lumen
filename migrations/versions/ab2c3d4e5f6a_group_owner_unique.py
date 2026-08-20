"""Enforce at most one owner per group

Revision ID: ab2c3d4e5f6a
Revises: aa1b2c3d4e5f
Create Date: 2026-08-19 00:00:00.000000

Adds a partial unique index on group_members(group_id) WHERE is_owner, so two
concurrent ownership transfers cannot both commit is_owner=True. Without it a
double-owner row pair makes get_group_owner()'s scalar_one_or_none() raise
MultipleResultsFound and every page of that group 500s until the rows are
fixed by hand. A separate revision (rather than an edit to aa1b2c3d4e5f)
because that revision has already been applied to running databases.

SQLite dev uses ``create_all`` + stamp head and never runs this chain, so
only the PostgreSQL path needs to be correct.
"""

import sqlalchemy as sa
from alembic import op

revision = "ab2c3d4e5f6a"
down_revision = "aa1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "uq_group_members_owner",
        "group_members",
        ["group_id"],
        unique=True,
        postgresql_where=sa.text("is_owner"),
        sqlite_where=sa.text("is_owner"),
    )


def downgrade():
    op.drop_index("uq_group_members_owner", table_name="group_members")
