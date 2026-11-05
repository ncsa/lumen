"""Add request_counts_hourly_total, the org-wide heatmap's per-hour totals

Revision ID: d7f9b1c3e5a7
Revises: c6e8a0b2d4f6, c5d7e9f1a3b6
Create Date: 2026-11-05 00:00:00.000000

The org-wide ``/api/usage/heatmap`` summed ``request_counts_hourly``, whose rows
are hours x models x sources (about 350k a year), although it needs only one
number per hour (#429). This adds a 1 hour hierarchical continuous aggregate over
``request_counts_hourly`` grouped by ``bucket`` alone, at most 8,760 rows a year:

    request_logs
      -> request_counts_hourly_by_entity     (1 hour, per entity)
        -> request_counts_hourly             (1 hour, org-wide)
          -> request_counts_hourly_total     (1 hour, org-wide totals; new)
          -> request_counts_daily            (1 day, org-wide)

It keeps all four totals (requests, tokens, cost), so ``check-aggregate``'s
cross-view comparison covers it like the others. Being hourly, it serves the
heatmap's hour-aligned window for every period exactly.

**Real-time** (``materialized_only = false``) in its own statement, as in
``c6e8a0b2d4f6``: the ``CREATE``'s ``WITH`` clause rejects it. Its real-time half
reads the hourly view, itself real-time, so the current hour shows through.

**Policy**: start 30 days, end 4 hours (one bucket behind the hourly view's 3
hours, so it only copies buckets its child has materialised), every hour.

**Outside ``reassign-model`` and ``erase-entity``.** It has neither
``model_config_id`` nor ``entity_id``, so their catalog walk never refreshes it.
That is correct: moving rows to another model or entity changes no hour's totals.

Created ``WITH NO DATA``; ``flask upgrade`` backfills it after its child.

This revision also merges the two heads ``c6e8a0b2d4f6`` and ``c5d7e9f1a3b6``,
both of which branched off ``c8d0e2f4a6b8``.

On SQLite there are no continuous aggregates and this migration does nothing.
"""

from alembic import op

revision = "d7f9b1c3e5a7"
down_revision = ("c6e8a0b2d4f6", "c5d7e9f1a3b6")
branch_labels = None
depends_on = None

VIEW = "request_counts_hourly_total"
CHILD = "request_counts_hourly"

_COMMENT = (
    "Hourly org-wide usage totals, grouped by bucket only, with request/token/cost "
    "totals; read by the org-wide /usage heatmap. A hierarchical aggregate over "
    "request_counts_hourly, so its totals equal the hourly view's. Real-time "
    "(materialized_only = false), so the current hour is included. Refresh it after "
    "request_counts_hourly. Deliberately outside the reassign-model and erase-entity "
    "refresh walks: it has neither model_config_id nor entity_id, and moving rows "
    "between models or entities changes no hour's totals."
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if not _is_postgresql():
        return

    op.execute(f"""
        CREATE MATERIALIZED VIEW {VIEW}
        WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('1 hour', bucket) AS bucket,
                CAST(SUM(requests) AS BIGINT)      AS requests,
                CAST(SUM(input_tokens) AS BIGINT)  AS input_tokens,
                CAST(SUM(output_tokens) AS BIGINT) AS output_tokens,
                SUM(cost)                          AS cost
            FROM {CHILD}
            GROUP BY 1
        WITH NO DATA
    """)
    # Separate statement: the CREATE's WITH clause rejects it.
    op.execute(f"ALTER MATERIALIZED VIEW {VIEW} SET (timescaledb.materialized_only = false)")
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{VIEW}',
            start_offset => INTERVAL '30 days',
            end_offset   => INTERVAL '4 hours',
            schedule_interval => INTERVAL '1 hour')
    """)
    # A continuous aggregate is a plain view (relkind 'v'): see g1h2i3j4k5l6.
    escaped = _COMMENT.replace("'", "''")
    op.execute(f"COMMENT ON VIEW {VIEW} IS '{escaped}'")


def downgrade():
    if not _is_postgresql():
        return

    # The policy has to go first: a job referencing the view blocks the DROP.
    op.execute(f"SELECT remove_continuous_aggregate_policy('{VIEW}', if_exists => true)")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW}")
