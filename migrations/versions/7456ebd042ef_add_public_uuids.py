"""Add an opaque public uuid to entities, groups, api_keys, conversations and auth_requests

Revision ID: 7456ebd042ef
Revises: f4a5b6c7d8e9
Create Date: 2026-11-14 00:00:00.000000

Integer primary keys are sequential and easy to guess, so they must not reach
a user (#560). Each table whose rows a user can address gets a random uuid,
which the app uses in URLs, JSON payloads, the CLI and the session instead of
the integer id. The uuid is for external use only: the integer id stays the
primary key and every foreign key still points at it.

On PostgreSQL each column is added in one statement as NOT NULL with the
server default gen_random_uuid(), which gives every existing row its own uuid,
followed by a unique index ix_<table>_uuid. The default stays on: 2.3.1 pods
keep serving during a rolling ``helm upgrade`` while the migration runs, and
their INSERTs leave out the uuid column, so without it they would fail with a
NOT NULL violation until they are replaced. The app still sets uuid4() itself.

Other dialects (SQLite in the unit tests) add the column nullable, give every
existing row its own uuid4() generated in Python, then make it NOT NULL with
the same unique index.
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "7456ebd042ef"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None

TABLES = ("entities", "groups", "api_keys", "conversations", "auth_requests")

COMMENT = (
    "Opaque public identifier for external use only (URLs, API payloads, CLI, session); "
    "never used as a key or join column"
)

# Rows updated per executemany round trip during the backfill.
_BATCH = 1000


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _backfill(conn, table_name):
    table = sa.table(table_name, sa.column("id", sa.Integer), sa.column("uuid", sa.Uuid))
    ids = conn.execute(sa.select(table.c.id).where(table.c.uuid.is_(None))).scalars().all()
    stmt = table.update().where(table.c.id == sa.bindparam("_id")).values(uuid=sa.bindparam("_uuid"))
    for start in range(0, len(ids), _BATCH):
        conn.execute(stmt, [{"_id": i, "_uuid": uuid.uuid4()} for i in ids[start:start + _BATCH]])


def upgrade():
    conn = op.get_bind()
    for table_name in TABLES:
        if _is_postgresql():
            op.add_column(table_name, sa.Column(
                "uuid", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()"),
            ))
            op.create_index(f"ix_{table_name}_uuid", table_name, ["uuid"], unique=True)
            op.execute(f"COMMENT ON COLUMN {table_name}.uuid IS $comment${COMMENT}$comment$")
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column("uuid", sa.Uuid(), nullable=True))
        _backfill(conn, table_name)
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column("uuid", existing_type=sa.Uuid(), nullable=False)
            batch_op.create_index(f"ix_{table_name}_uuid", ["uuid"], unique=True)


def downgrade():
    for table_name in reversed(TABLES):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_uuid")
            batch_op.drop_column("uuid")
