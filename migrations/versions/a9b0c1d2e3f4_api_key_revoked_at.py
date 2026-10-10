"""Replace api_keys.active with revoked_at

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-10-10 00:00:00.000000

Adds a nullable ``revoked_at`` (naive UTC) to ``api_keys`` and drops the
``active`` flag; a null ``revoked_at`` now means the key is usable. Keys that
were already inactive have no recorded revocation time, so they are backfilled
with ``COALESCE(last_used_at, created_at)`` -- the latest time the key is known
to have been live; a row with neither falls back to the migration time so it
stays revoked. The downgrade restores ``active`` as ``revoked_at IS NULL``.
batch_alter_table keeps the statements portable; SQLite dev uses
``create_all`` + stamp head and never runs this chain.
"""

import sqlalchemy as sa
from alembic import op

from lumen.timeutils import utcnow

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

_REVOKED_AT_COMMENT = (
    "UTC time the key was revoked; null while the key is usable; "
    "approximate for keys revoked before this column existed"
)
_ACTIVE_COMMENT = "Inactive keys are rejected on all requests"


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("revoked_at", sa.DateTime(), nullable=True))

    op.execute(
        sa.text(
            "UPDATE api_keys SET revoked_at = COALESCE(last_used_at, created_at, :now) "
            "WHERE active = false"
        ).bindparams(now=utcnow())
    )

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("active")

    if _is_postgresql():
        op.execute(f"COMMENT ON COLUMN api_keys.revoked_at IS {_q(_REVOKED_AT_COMMENT)}")


def downgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("active", sa.Boolean(), nullable=True))

    op.execute("UPDATE api_keys SET active = (revoked_at IS NULL)")

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column("active", existing_type=sa.Boolean(), nullable=False)
        batch_op.drop_column("revoked_at")

    if _is_postgresql():
        op.execute(f"COMMENT ON COLUMN api_keys.active IS {_q(_ACTIVE_COMMENT)}")
