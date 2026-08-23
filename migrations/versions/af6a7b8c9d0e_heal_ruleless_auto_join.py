"""Heal rule-less auto-join groups

Revision ID: af6a7b8c9d0e
Revises: ae5f6a7b8c9d
Create Date: 2026-08-20 00:00:00.000000

Auto-join is cleared on any group with zero rules — the ac3 carry-over
(auto_join = config_managed) marked groups that never had rules (the old
"ensure the group exists" idiom in config.yaml); those matched nobody,
lost members at each login, and refused manual membership edits.

The config.yaml group_rules section is not read at all: Lumen 2.0 removed it
(auto-join rules are database rows managed on each group's Rules tab), so
there is nothing to import here. An earlier draft of this revision imported
the section once; that import is gone — operators recreate any rules they
still want in the UI.

Downgrade is a no-op: the heal cannot know which groups it touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "af6a7b8c9d0e"
down_revision = "ae5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade():
    # An auto-join group with no rules matches nobody (fail-closed), drains its
    # members at each login, and blocks manual membership edits.
    op.get_bind().execute(sa.text(
        "UPDATE groups SET auto_join = false "
        "WHERE auto_join = true AND id NOT IN (SELECT DISTINCT group_id FROM group_rules)"
    ))


def downgrade():
    pass
