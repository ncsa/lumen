"""Add surrogate PK to request_logs; make time a regular indexed column

Revision ID: y9z0a1b2c3d4
Revises: x8y9z0a1b2c3
Create Date: 2026-05-16 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "y9z0a1b2c3d4"
down_revision = "x8y9z0a1b2c3"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    # On PostgreSQL the table was created without a PK (TimescaleDB hypertable).
    # On SQLite the table was created with PrimaryKeyConstraint('time').

    # 1. Drop the existing primary key on `time` (SQLite only)
    if not _is_postgresql():
        op.drop_constraint("request_logs_pkey", "request_logs", type_="primary")

    # 2. Add the surrogate bigint PK column
    # Must add nullable first — TimescaleDB propagates ADD COLUMN to all chunks
    # and rejects NOT NULL without a default when chunks have existing rows.
    op.add_column(
        "request_logs",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            nullable=True,
            comment="Surrogate PK; avoids timestamp collision under concurrent load",
        ),
    )

    if _is_postgresql():
        # Create sequence, backfill existing rows, wire up default, then tighten
        op.execute("CREATE SEQUENCE IF NOT EXISTS request_logs_id_seq")
        op.execute("UPDATE request_logs SET id = nextval('request_logs_id_seq')")
        op.execute("ALTER SEQUENCE request_logs_id_seq OWNED BY request_logs.id")
        op.execute("ALTER TABLE request_logs ALTER COLUMN id SET DEFAULT nextval('request_logs_id_seq')")
        op.execute("ALTER TABLE request_logs ALTER COLUMN id SET NOT NULL")
    else:
        op.execute("UPDATE request_logs SET id = rowid")
        op.alter_column("request_logs", "id", nullable=False)

    # 3. Add new primary key on id
    op.create_primary_key("request_logs_pkey", "request_logs", ["id"])

    # 4. Add index on time (was implicitly covered by old PK)
    op.create_index("ix_request_logs_time", "request_logs", ["time"])


def downgrade():
    op.drop_index("ix_request_logs_time", table_name="request_logs")
    op.drop_constraint("request_logs_pkey", "request_logs", type_="primary")
    op.drop_column("request_logs", "id")
    if not _is_postgresql():
        op.create_primary_key("request_logs_pkey", "request_logs", ["time"])
