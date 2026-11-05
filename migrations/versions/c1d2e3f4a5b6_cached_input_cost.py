"""Add cached_input_cost_per_million to model_configs

Revision ID: c1d2e3f4a5b6
Revises: e8a0c2d4f6b8
Create Date: 2026-11-02 00:00:00.000000

Providers that run prompt caching report how many input tokens they served from
their cache, and many charge a different (usually far lower) rate for those
tokens. ``calculate_cost`` now prices that part of the prompt separately once
this rate is set.

NULL means "cached input costs the same as ordinary input", which is what
billing did before this column existed, so the upgrade on its own changes no
price. A rate of 0 is meaningful and distinct: the provider charges nothing for
cache reads. No backfill sets a rate for existing rows — only an admin knows
what each backend charges.
"""

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "e8a0c2d4f6b8"
branch_labels = None
depends_on = None

_COMMENT = (
    "USD cost per one million cached input tokens; "
    "NULL = same as input_cost_per_million"
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.add_column(
            sa.Column("cached_input_cost_per_million", sa.Numeric(12, 6), nullable=True)
        )

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN model_configs.cached_input_cost_per_million IS "
            f"$comment${_COMMENT}$comment$"
        )


def downgrade():
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.drop_column("cached_input_cost_per_million")
