"""Document that an unset cache-write price means 0

Revision ID: 2ea58823bdc7
Revises: 7456ebd042ef
Create Date: 2026-11-19 00:00:00.000000

model_configs.cache_write_cost_per_million stays nullable and NULL stays the
stored "unset" value, but an unset rate now bills reported cache-write tokens
at 0 instead of the input rate (#678). Only the PostgreSQL column comment
changes; there is no data migration.
"""

from alembic import op

revision = "2ea58823bdc7"
down_revision = "7456ebd042ef"
branch_labels = None
depends_on = None

_NEW_COMMENT = "USD cost per one million cache-write input tokens; NULL = 0 (cache writes are free)"
_OLD_COMMENT = (
    "USD cost per one million cache-write input tokens; NULL = same as input_cost_per_million"
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _set_comment(comment):
    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN model_configs.cache_write_cost_per_million IS $comment${comment}$comment$"
        )


def upgrade():
    _set_comment(_NEW_COMMENT)


def downgrade():
    _set_comment(_OLD_COMMENT)
