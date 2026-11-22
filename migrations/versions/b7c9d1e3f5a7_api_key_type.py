"""Add api_keys.key_type ('normal', 'admin' or 'monitoring')

Revision ID: b7c9d1e3f5a7
Revises: 2ea58823bdc7
Create Date: 2026-11-22 00:00:00.000000

The column is added with ``server_default='normal'``, so every existing key
(user, project and OAuth keys alike) becomes a normal key and behaves exactly
as before. An explicit backfill UPDATE covers any row the default did not fill
(SQLite), then NOT NULL and the CHECK ``ck_api_keys_key_type`` are added.
batch_alter_table keeps the statements portable.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c9d1e3f5a7"
down_revision = "2ea58823bdc7"
branch_labels = None
depends_on = None

_COMMENT = (
    "Key type: 'normal' (proxy API, billed), 'admin' (admin API and model listing) "
    "or 'monitoring' (model listing only); the type decides access, never the key prefix"
)


def upgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("key_type", sa.String(16), nullable=True, server_default="normal"))
    op.execute("UPDATE api_keys SET key_type = 'normal' WHERE key_type IS NULL")
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column(
            "key_type", existing_type=sa.String(16), existing_server_default="normal",
            nullable=False, comment=_COMMENT,
        )
        batch_op.create_check_constraint(
            "ck_api_keys_key_type", "key_type IN ('normal', 'admin', 'monitoring')",
        )


def downgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_constraint("ck_api_keys_key_type", type_="check")
        batch_op.drop_column("key_type")
