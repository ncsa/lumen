"""Add api_keys.expires_at and its index

Revision ID: b1c2d3e4f5a6
Revises: c93d4de8245c
Create Date: 2026-10-16 00:00:00.000000

Adds a nullable ``expires_at`` (naive UTC) to ``api_keys``; NULL means the key
never expires, so existing keys keep working unchanged. Indexed for the
background expirer, which revokes overdue keys by setting ``revoked_at`` to
``expires_at``. Also notes key expiry among the system revocations in the
``revoked_by_entity_id`` comment.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "c93d4de8245c"
branch_labels = None
depends_on = None

_EXPIRES_AT_COMMENT = (
    "UTC time the key expires and is revoked (revoked_at set to this value); "
    "null if the key never expires"
)
_REVOKED_BY_COMMENT = (
    "Entity (user) that revoked this key; null while usable, for keys revoked before "
    "this column existed, for system revocations (OAuth code replay, key expiry), or if the revoker was deleted"
)
_OLD_REVOKED_BY_COMMENT = (
    "Entity (user) that revoked this key; null while usable, for keys revoked before "
    "this column existed, for system revocations (OAuth code replay), or if the revoker was deleted"
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
    op.create_index("ix_api_keys_expires_at", "api_keys", ["expires_at"])

    if _is_postgresql():
        op.execute(f"COMMENT ON COLUMN api_keys.expires_at IS {_q(_EXPIRES_AT_COMMENT)}")
        op.execute(f"COMMENT ON COLUMN api_keys.revoked_by_entity_id IS {_q(_REVOKED_BY_COMMENT)}")


def downgrade():
    op.drop_index("ix_api_keys_expires_at", table_name="api_keys")
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("expires_at")

    if _is_postgresql():
        op.execute(f"COMMENT ON COLUMN api_keys.revoked_by_entity_id IS {_q(_OLD_REVOKED_BY_COMMENT)}")
