"""Add auth_requests.ttl

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-10-17 00:00:00.000000

Adds a nullable ``ttl`` (seconds) to ``auth_requests``: the key TTL an OAuth
key request asked for (NULL when it asked for none), replaced by the
approver's choice at approval. ``_mint`` counts the key's ``expires_at`` from
it. Existing rows stay NULL and mint with the ``api.keys.ttl`` cap.
BIGINT, because an allowed TTL (up to 100 years) does not fit a 32-bit integer.
"""

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None

_TTL_COMMENT = (
    "Key TTL in seconds (0 = never): as requested (null if none was), "
    "replaced by the approver's choice at approval"
)


def upgrade():
    with op.batch_alter_table("auth_requests") as batch_op:
        batch_op.add_column(sa.Column("ttl", sa.BigInteger(), nullable=True, comment=_TTL_COMMENT))


def downgrade():
    with op.batch_alter_table("auth_requests") as batch_op:
        batch_op.drop_column("ttl")
