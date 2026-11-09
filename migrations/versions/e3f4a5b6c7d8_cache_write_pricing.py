"""Record cache-write pricing and tokens in model_configs, request_logs and the stats totals

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-11-06 00:00:00.000000

Providers that run prompt caching price the tokens they serve from the cache
('cached input') separately from the tokens they had to process and store when
the cache misses ('cache write'). The read side arrived in d2e3f4a5b6c7 and
c1d2e3f4a5b6; this change adds the write side everywhere the read exists:

* model_configs.cache_write_cost_per_million — USD cost per one million
  input tokens written into the provider's prompt cache. NULL = charge the
  ordinary input rate for them, so billing is unchanged until an admin sets it.
* request_logs.cache_write_tokens — the part of input_tokens written
  into the cache. NULL when the backend reported no such count: an aborted
  request with no terminal usage object, a row written before this column
  existed, or an upstream without cache-write accounting. The non-cached
  remainder of the prompt is never inferred as a write; a NULL keeps billing
  that remainder at the input rate, so the column records what the backend
  said rather than what the price assumed.
* model_stats / entity_stats — a running cache_write_tokens total
  plus its folded_* part, so flask check-stats compares the fold's share
  with request_logs exactly as it does for the other token totals.

Only storage lands here, mirroring d2e3f4a5b6c7; the continuous aggregates and
the monitor's token windows keep their current definitions.
"""

import sqlalchemy as sa
from alembic import op

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None

_PRICE_COMMENT = (
    "USD cost per one million cache-write input tokens; NULL = same as input_cost_per_million"
)
_LOG_COMMENT = (
    "Cache-write part of input_tokens; NULL when no upstream usage was available"
)
_MODEL_COMMENT = (
    "Total cache-write tokens among input_tokens (NULL rows in request_logs add nothing)"
)
_FOLDED_COMMENT = "Part of cache_write_tokens added by the stats fold since the cutover"


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    # Nullable and no default, mirroring cached_input_cost_per_million: existing
    # models keep charging the input rate until an admin sets the write price.
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.add_column(sa.Column("cache_write_cost_per_million", sa.Numeric(12, 6), nullable=True))

    # Nullable and no default: existing rows stay NULL rather than claiming a
    # zero cache write. On PostgreSQL this is a metadata-only change (no
    # rewrite, no table scan), which is what makes it affordable on the
    # request_logs hypertable.
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))

    # The totals are counters, so they start at 0 for every existing row; only
    # rows written from now on can carry a count.
    for table in ("model_stats", "entity_stats"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0")
            )
            batch_op.add_column(
                sa.Column("folded_cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0")
            )

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN model_configs.cache_write_cost_per_million IS $comment${_PRICE_COMMENT}$comment$"
        )
        op.execute(
            f"COMMENT ON COLUMN request_logs.cache_write_tokens IS $comment${_LOG_COMMENT}$comment$"
        )
        for table in ("model_stats", "entity_stats"):
            op.execute(
                f"COMMENT ON COLUMN {table}.cache_write_tokens IS $comment${_MODEL_COMMENT}$comment$"
            )
            op.execute(
                f"COMMENT ON COLUMN {table}.folded_cache_write_tokens IS $comment${_FOLDED_COMMENT}$comment$"
            )


def downgrade():
    for table in ("model_stats", "entity_stats"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("folded_cache_write_tokens")
            batch_op.drop_column("cache_write_tokens")

    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.drop_column("cache_write_tokens")

    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.drop_column("cache_write_cost_per_million")
