"""Convert drifted timestamptz columns to naive UTC (TIMESTAMP WITHOUT TIME ZONE)

Databases created before Alembic carry these columns as ``timestamptz``, while
the models and the initial schema declare them naive. psycopg hands back aware
datetimes for them, which crash against naive app timestamps (``/profile`` in
2.3.0). Only columns that are currently ``timestamptz`` are converted, so
databases built from the migrations are untouched.

Revision ID: b8c0d2e4f6a1
Revises: f9b1d3e5a7c9
Create Date: 2026-11-06 00:00:00.000000

"""

from alembic import op

revision = "b8c0d2e4f6a1"
down_revision = "f9b1d3e5a7c9"
branch_labels = None
depends_on = None

COLUMNS = {
    "api_keys": ("created_at", "last_used_at"),
    "conversations": ("created_at", "updated_at"),
    "entities": ("created_at",),
    "groups": ("created_at",),
    "messages": ("created_at",),
    "model_configs": ("created_at",),
    "model_endpoints": ("created_at", "last_checked_at"),
    "model_stats": ("last_used_at",),
}


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, columns in COLUMNS.items():
        for column in columns:
            is_aware = bind.exec_driver_sql(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s AND data_type = 'timestamp with time zone'",
                (table, column),
            ).first()
            if is_aware:
                op.execute(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE timestamp '
                    f"USING \"{column}\" AT TIME ZONE 'UTC'"
                )


def downgrade():
    # The original type of each column is not recorded, and naive is the intended one.
    pass
