"""Add request_logs.api_key_id, a NOT VALID foreign key to api_keys

Revision ID: c5d7e9f1a3b6
Revises: c8d0e2f4a6b8
Create Date: 2026-11-03 00:00:00.000000

Records which API key paid for each request (#407). API requests will get the
key's id; chat requests stay NULL (``source`` already marks them), and so do
rows logged before this column existed, whose key nobody recorded.

On PostgreSQL the column and its foreign key are added in two statements so
that neither scans the history:

1. ``ADD COLUMN api_key_id INTEGER``: nullable with no default, a
   catalogue-only change that rewrites no data and works on a hypertable with
   compressed chunks (``i3j4k5l6m7n8``), like ``f5b6c7d8e9f0``.
2. ``ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID``: new INSERTs and UPDATEs
   are checked from now on, but existing rows are not. The plain
   ``ADD COLUMN ... REFERENCES`` form would validate every existing row,
   decompressing every compressed chunk of the never-pruned hypertable while
   holding ACCESS EXCLUSIVE on it, and so block all billing for the length of
   that scan. Skipping the check loses nothing: the column was created a
   moment earlier and every existing row is NULL.

The foreign key is ``ON DELETE RESTRICT``, like the other ``request_logs``
foreign keys since ``c8d0e2f4a6b8``: keys are revoked, never deleted, and
``flask erase-entity`` keeps an erased user's keys under the ghost, so a
direct ``DELETE`` of a key with request history fails instead of rewriting
the compressed hypertable. ``api_key_id`` has no index, so that refusal scans
every chunk first, like the endpoint case; no index is added for a path that
should never run.

The constraint is never validated, here or later, and stays
``convalidated = false`` on purpose. ``VALIDATE CONSTRAINT`` would be the same
full-history scan, and it would prove nothing, because the only rows it would
check are the NULL ones.

On SQLite the column and the foreign key are added through
``batch_alter_table``; like every foreign key there, it is not enforced.
"""

import sqlalchemy as sa
from alembic import op

revision = "c5d7e9f1a3b6"
down_revision = "c8d0e2f4a6b8"
branch_labels = None
depends_on = None

_FK = "fk_request_logs_api_key_id"
_COMMENT = (
    "API key that made the request; NULL for chat (see source) and for API "
    "requests logged before this column existed. RESTRICT on delete: keys are "
    "revoked, never deleted"
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if _is_postgresql():
        op.execute("ALTER TABLE request_logs ADD COLUMN api_key_id INTEGER")
        # NOT VALID: see the module docstring. Never followed by VALIDATE CONSTRAINT.
        op.execute(
            f"ALTER TABLE request_logs ADD CONSTRAINT {_FK} "
            "FOREIGN KEY (api_key_id) REFERENCES api_keys (id) ON DELETE RESTRICT NOT VALID"
        )
        op.execute(f"COMMENT ON COLUMN request_logs.api_key_id IS $comment${_COMMENT}$comment$")
    else:
        with op.batch_alter_table("request_logs") as batch_op:
            batch_op.add_column(sa.Column("api_key_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(_FK, "api_keys", ["api_key_id"], ["id"], ondelete="RESTRICT")


def downgrade():
    if _is_postgresql():
        op.execute(f"ALTER TABLE request_logs DROP CONSTRAINT IF EXISTS {_FK}")
        op.execute("ALTER TABLE request_logs DROP COLUMN api_key_id")
    else:
        with op.batch_alter_table("request_logs") as batch_op:
            batch_op.drop_column("api_key_id")
