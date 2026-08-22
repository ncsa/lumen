"""Drop the implicit 'default' group

Revision ID: ae5f6a7b8c9d
Revises: ad4e5f6a7b8c
Create Date: 2026-08-19 18:00:00.000000

The login sync used to auto-join every user to a group literally named
"default" — an implicit everyone-group from before auto-join rules existed.
That special case is gone from the code, so the group and its auto-created
memberships (plus any coin pool, rules, or model grants attached to it) are
removed here; the delete cascades through group_members, group_limits,
group_rules, and model_group_access. Coin budgets for users without a group
pool come from config.yaml's defaults.tokens.

Guarded: the group is deleted only when it looks machine-generated — no
model grants and no usable coin pool (no group_limits row, or max_coins = 0).
A deployment that deliberately attached policy to its 'default' group keeps
it as an ordinary group (no longer auto-joined) for the operator to dispose
of; a warning-worthy state, but never silent data destruction.

Irreversible: the membership rows were machine-generated at login and carry
no information worth restoring, so downgrade is a no-op.
"""

import sqlalchemy as sa
from alembic import op

revision = "ae5f6a7b8c9d"
down_revision = "ad4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        """
        DELETE FROM groups
        WHERE name = 'default'
          AND id NOT IN (SELECT group_id FROM model_group_access)
          AND id NOT IN (SELECT group_id FROM group_limits WHERE max_coins != 0)
        """
    ))


def downgrade():
    # The implicit everyone-group cannot be meaningfully reconstructed.
    pass
