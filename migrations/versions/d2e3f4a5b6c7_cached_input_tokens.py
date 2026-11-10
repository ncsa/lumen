"""Record cached input tokens in request_logs and the stats totals

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-02 00:00:00.000000

Providers that run prompt caching report how many input tokens they served from
their cache, and ``calculate_cost`` prices that part of the prompt at the
model's ``cached_input_cost_per_million``. Until now the count itself was
dropped after pricing, so nothing downstream could tell a discounted request
from a full-price one: the stored ``input_tokens`` is the whole prompt, and the
billed cost cannot be reconstructed from it with the published rates.

This stores the count, so the split survives:

* ``request_logs.cached_input_tokens`` — NULL when there is nothing to record: an
  aborted request with no terminal usage object, or a row written before this
  column existed. A usage object the upstream did send without cache fields
  records 0, which is the same claim the cost makes for it.
* ``model_stats`` / ``entity_stats`` — a running ``cached_input_tokens`` total
  plus its ``folded_*`` part, so ``flask check-stats`` compares the fold's share
  with ``request_logs`` exactly as it does for the other token totals.

Only storage lands here. The TimescaleDB continuous aggregates that feed the
usage charts and the monitor's token windows keep their current definitions:
adding a field to a continuous aggregate means dropping and recreating it, so
that rebuild is deferred to its own change — see
``docs/architecture.md`` ("Cached-token telemetry"). The history this migration
starts recording is what makes that follow-up a pure aggregate-definition change
rather than a data loss.

No backfill: a count nobody recorded cannot be invented, and NULL already says
so.
"""

import sqlalchemy as sa
from alembic import op

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

_LOG_COMMENT = (
    "Cached part of input_tokens; NULL when no upstream usage was available"
)
_MODEL_COMMENT = (
    "Total cached input tokens among input_tokens (NULL rows in request_logs add nothing)"
)
_FOLDED_COMMENT = "Part of cached_input_tokens added by the stats fold since the cutover"


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    # Nullable and no default: existing rows stay NULL rather than claiming a
    # zero cache read. On PostgreSQL this is a metadata-only change (no rewrite,
    # no table scan), which is what makes it affordable on the request_logs
    # hypertable.
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.add_column(sa.Column("cached_input_tokens", sa.Integer(), nullable=True))

    # The totals are counters, so they start at 0 for every existing row; only
    # rows written from now on can carry a count.
    for table in ("model_stats", "entity_stats"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False, server_default="0")
            )
            batch_op.add_column(
                sa.Column("folded_cached_input_tokens", sa.BigInteger(), nullable=False, server_default="0")
            )

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN request_logs.cached_input_tokens IS $comment${_LOG_COMMENT}$comment$"
        )
        for table in ("model_stats", "entity_stats"):
            op.execute(
                f"COMMENT ON COLUMN {table}.cached_input_tokens IS $comment${_MODEL_COMMENT}$comment$"
            )
            op.execute(
                f"COMMENT ON COLUMN {table}.folded_cached_input_tokens IS $comment${_FOLDED_COMMENT}$comment$"
            )


def downgrade():
    for table in ("model_stats", "entity_stats"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("folded_cached_input_tokens")
            batch_op.drop_column("cached_input_tokens")

    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.drop_column("cached_input_tokens")
