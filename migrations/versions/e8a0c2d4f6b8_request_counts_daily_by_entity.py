"""Add request_counts_daily_by_entity, the per-entity Year and All ranges' daily view

Revision ID: e8a0c2d4f6b8
Revises: d7f9b1c3e5a7
Create Date: 2026-11-05 00:00:00.000000

The per-entity ``/usage`` charts read ``request_counts_hourly_by_entity`` for
every range, Year and All included, at hourly granularity (#429). This adds a
1 day hierarchical continuous aggregate over it, grouped by the same keys:

    request_logs
      -> request_counts_hourly_by_entity     (1 hour, per entity)
        -> request_counts_daily_by_entity    (1 day, per entity; new)
        -> request_counts_hourly             (1 hour, org-wide)
          -> request_counts_hourly_total     (1 hour, org-wide totals)
          -> request_counts_daily            (1 day, org-wide)

It keeps the NULL ``entity_id`` group of deleted users, so its totals equal the
entity aggregate's, and it keeps all four totals (requests, tokens, cost), so
``check-aggregate``'s cross-view comparison covers it like the others. The Year
window starts at a UTC midnight, so the daily buckets serve it exactly. Buckets
are UTC days, whatever the session time zone. ``SUM`` of a bigint is NUMERIC, so
the counts are cast back to bigint.

**Real-time** (``materialized_only = false``) in its own statement, as in
``c6e8a0b2d4f6``: the ``CREATE``'s ``WITH`` clause rejects it. Its real-time half
reads the entity aggregate, itself real-time, so today shows through.

**Policy**: start 30 days, end 1 day, every hour, as ``request_counts_daily``.

It carries both ``model_config_id`` and ``entity_id``, so ``reassign-model`` and
``erase-entity`` refresh it after its child.

Created ``WITH NO DATA``; ``flask upgrade`` backfills it after its child.

On SQLite there are no continuous aggregates and this migration does nothing.
"""

from alembic import op

revision = "e8a0c2d4f6b8"
down_revision = "d7f9b1c3e5a7"
branch_labels = None
depends_on = None

VIEW = "request_counts_daily_by_entity"
CHILD = "request_counts_hourly_by_entity"

_COMMENT = (
    "Daily per-entity usage, grouped by (bucket, entity_id, model_config_id, "
    "source), with request/token/cost totals; buckets are UTC days. A "
    "hierarchical aggregate over request_counts_hourly_by_entity, for the Year "
    "and All per-entity /usage ranges; it keeps the NULL entity_id group, so its "
    "totals equal the hourly view's. Real-time (materialized_only = false), so "
    "today is included. Refresh it after request_counts_hourly_by_entity."
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
                time_bucket('1 day', bucket) AS bucket,
                entity_id,
                model_config_id,
                source,
                CAST(SUM(requests) AS BIGINT)      AS requests,
                CAST(SUM(input_tokens) AS BIGINT)  AS input_tokens,
                CAST(SUM(output_tokens) AS BIGINT) AS output_tokens,
                SUM(cost)                          AS cost
            FROM {CHILD}
            GROUP BY 1, 2, 3, 4
        WITH NO DATA
    """)
    # Separate statement: the CREATE's WITH clause rejects it.
    op.execute(f"ALTER MATERIALIZED VIEW {VIEW} SET (timescaledb.materialized_only = false)")
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{VIEW}',
            start_offset => INTERVAL '30 days',
            end_offset   => INTERVAL '1 day',
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
