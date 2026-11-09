"""Rebuild request_counts_hourly over the entity aggregate; add request_counts_daily

Revision ID: c6e8a0b2d4f6
Revises: c8d0e2f4a6b8
Create Date: 2026-11-04 00:00:00.000000

``request_counts_hourly`` and ``request_counts_hourly_by_entity`` each scanned raw
``request_logs`` on every refresh, and nothing tied their totals together (#177).
This makes the org-wide views hierarchical continuous aggregates:

    request_logs
      -> request_counts_hourly_by_entity   (1 hour, per entity; g1h2i3j4k5l6)
        -> request_counts_hourly           (1 hour, org-wide; rebuilt here)
          -> request_counts_daily          (1 day, org-wide; new)

Both org-wide views are sums of sums over the entity aggregate's rows, which keep
the NULL ``entity_id`` group of deleted users, so their totals for any window of
whole buckets equal the sum over ``request_counts_hourly_by_entity``, and that
equals raw ``request_logs``. Integer and NUMERIC sums are exact. ``SUM`` of a
bigint is NUMERIC, so the counts are cast back to bigint to keep the column
types of the view this replaces.

**Real-time** (``materialized_only = false``) is set on both views, each in its
own statement: the ``WITH`` clause of the ``CREATE`` rejects it. A parent's
real-time half reads the child *view*, which is real-time itself, so a request
made in the current hour shows through both views before any refresh.

**Refresh order.** Below the child's watermark a parent refresh reads the child's
*materialised* rows, not raw ``request_logs``, so refreshing a parent before its
child copies the child's stale rows there (measured on 2.27.2). The policies keep
each parent's watermark on buckets the child has already materialised:

  hourly  start 30 days (the child's), end 3 hours (the child lags up to about
          2 hours, plus one bucket), every hour
  daily   start 30 days, end 1 day, every hour

A long ``start_offset`` is cheap here: refresh is driven by the invalidation log,
so only buckets the child rewrote are recomputed.

**Rebuilt in place, ``WITH NO DATA``.** Until ``flask upgrade`` backfills them
(month by month, child first, from the child's materialised rows rather than raw
rows) the watermarks sit at -infinity and real-time aggregation still returns
correct totals, only slower. ``refresh_continuous_aggregate`` cannot run inside
the migration's transaction anyway.

No retention policy on either view, and nothing here touches ``request_logs``.

**Downgrade** drops ``request_counts_daily`` and restores the raw-based hourly
view exactly as ``i9j0k1l2m3n4`` and ``j4k5l6m7n8o9`` left it: same definition,
the 3 hour / 1 hour policy, real-time, and the comment. It does not
re-materialise; run ``flask backfill-aggregate --name request_counts_hourly``.

On SQLite there are no continuous aggregates and this migration does nothing.
"""

from alembic import op

revision = "c6e8a0b2d4f6"
down_revision = "c8d0e2f4a6b8"
branch_labels = None
depends_on = None

ENTITY = "request_counts_hourly_by_entity"
HOURLY = "request_counts_hourly"
DAILY = "request_counts_daily"

_HOURLY_COMMENT = (
    "Hourly org-wide usage, grouped by (bucket, model_config_id, source), with "
    "request/token/cost totals. A hierarchical aggregate over "
    "request_counts_hourly_by_entity, so its totals equal the per-entity sums, "
    "deleted users included. Real-time (materialized_only = false), so the "
    "current hour is included. Refresh it after request_counts_hourly_by_entity."
)

_DAILY_COMMENT = (
    "Daily org-wide usage, grouped by (bucket, model_config_id, source), with "
    "request/token/cost totals; buckets are UTC days. A hierarchical aggregate "
    "over request_counts_hourly, for the Year and All /usage ranges. Real-time "
    "(materialized_only = false), so today is included. Refresh it after "
    "request_counts_hourly."
)

# The view as j4k5l6m7n8o9 left it, restored by downgrade().
_RAW_HOURLY_COMMENT = (
    "Hourly org-wide rollup of request_logs, grouped by (bucket, "
    "model_config_id, source), carrying request/token/cost totals. No "
    "entity_id: the per-entity /usage charts read "
    "request_counts_hourly_by_entity instead. Real-time "
    "(materialized_only = false), so the current hour is included."
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _create_rollup(view, width, child):
    op.execute(f"""
        CREATE MATERIALIZED VIEW {view}
        WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('{width}', bucket) AS bucket,
                model_config_id,
                source,
                CAST(SUM(requests) AS BIGINT)      AS requests,
                CAST(SUM(input_tokens) AS BIGINT)  AS input_tokens,
                CAST(SUM(output_tokens) AS BIGINT) AS output_tokens,
                SUM(cost)                          AS cost
            FROM {child}
            GROUP BY 1, 2, 3
        WITH NO DATA
    """)
    # Separate statement: the CREATE's WITH clause rejects it.
    op.execute(f"ALTER MATERIALIZED VIEW {view} SET (timescaledb.materialized_only = false)")


def _comment(view, comment):
    # A COMMENT takes no bind parameters; double any quote so the literal stays closed.
    escaped = comment.replace("'", "''")
    op.execute(f"COMMENT ON VIEW {view} IS '{escaped}'")


def _drop(view):
    # The policy has to go first: a job referencing the view blocks the DROP.
    op.execute(f"SELECT remove_continuous_aggregate_policy('{view}', if_exists => true)")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view}")


def upgrade():
    if not _is_postgresql():
        return

    _drop(HOURLY)

    _create_rollup(HOURLY, "1 hour", ENTITY)
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{HOURLY}',
            start_offset => INTERVAL '30 days',
            end_offset   => INTERVAL '3 hours',
            schedule_interval => INTERVAL '1 hour')
    """)
    # A continuous aggregate is a plain view (relkind 'v'): see g1h2i3j4k5l6.
    _comment(HOURLY, _HOURLY_COMMENT)

    _create_rollup(DAILY, "1 day", HOURLY)
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{DAILY}',
            start_offset => INTERVAL '30 days',
            end_offset   => INTERVAL '1 day',
            schedule_interval => INTERVAL '1 hour')
    """)
    _comment(DAILY, _DAILY_COMMENT)


def downgrade():
    if not _is_postgresql():
        return

    _drop(DAILY)
    _drop(HOURLY)

    # i9j0k1l2m3n4's definition and policy.
    op.execute(f"""
        CREATE MATERIALIZED VIEW {HOURLY}
        WITH (timescaledb.continuous) AS
        SELECT
            time_bucket('1 hour', time) AS bucket,
            model_config_id,
            source,
            COUNT(*)          AS requests,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(cost)         AS cost
        FROM request_logs
        GROUP BY 1, 2, 3
        WITH NO DATA
    """)
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{HOURLY}',
            start_offset => INTERVAL '3 hours',
            end_offset   => INTERVAL '1 hour',
            schedule_interval => INTERVAL '1 hour')
    """)
    # j4k5l6m7n8o9's real-time setting and comment.
    op.execute(f"ALTER MATERIALIZED VIEW {HOURLY} SET (timescaledb.materialized_only = false)")
    _comment(HOURLY, _RAW_HOURLY_COMMENT)
