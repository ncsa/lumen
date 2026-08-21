"""Add the request_counts_hourly_by_entity continuous aggregate

Revision ID: g1h2i3j4k5l6
Revises: f7a8b9c0d1e2
Create Date: 2026-08-18 00:00:00.000000

``request_counts_hourly`` groups by ``(bucket, model_config_id, source)`` and
has no ``entity_id``. That single omission is why every per-user chart on
``/usage`` still scans raw ``request_logs``: there is nothing else to read. So
the moment a retention policy starts dropping raw chunks, **every individual's
"All Time" history is truncated while the org-wide charts, fed by the existing
aggregate, keep going**. That asymmetry is what gets reported as data loss, and
it is why this aggregate has to exist before compression and long before
retention.

**The bucket is one hour, and that is not a cost decision.** The heatmap does
``EXTRACT(HOUR FROM bucket)``; on a daily bucket every row collapses to hour 0
and the 7x24 grid silently becomes a single column. No error, just a wrong
chart. The periods the UI offers are 1 day / 1 week / 1 month, all >= 1 hour, so
an hourly bucket serves all of them.

**The measures are enumerated rather than copied.** ``request_counts_hourly``
carries only ``COUNT(*)`` and three sums — not even ``duration``. Once raw
chunks are dropped, anything absent here is unrecoverable, so this view also
carries the duration and TTFT material the per-model pages promise:

  duration_sum / duration_max   mean and worst case per bucket
  aborts                        COUNT(*) FILTER (WHERE outcome = 'disconnect')
  ttft_count / ttft_sum / ttft_max
  ttft_le_0_5 ... ttft_le_30    cumulative histogram buckets

``timescaledb_toolkit`` is not installed on the deployed image, so
``percentile_agg`` does not exist and percentiles come from fixed-edge counts
instead. The ``ttft_le_*`` columns are **cumulative**, in the Prometheus
histogram sense: each counts everything at or below its edge, so p95 is found by
walking the edges and interpolating. ``ttft_count`` is the denominator, and it
is ``COUNT(ttft_visible)`` rather than ``COUNT(*)`` on purpose — rows written
before ``f7a8b9c0d1e2`` have NULL ``ttft_visible``, a ``FILTER`` on NULL is
false, and counting them in the denominator would make every historical bucket
look uniformly slow. NULL is "not measured", and it is in none of the buckets.

**``materialized_only`` is set to false, explicitly, in its own statement.**
TimescaleDB 2.13 flipped that default to ``true``, and the deployed 2.27.2
demonstrably hides the current bucket with it: with ``end_offset => 1 hour`` and
an hourly schedule, a default aggregate returns nothing newer than up to two
hours ago. A student who runs forty requests in a 9 a.m. lab and opens
``/usage`` at 09:50 would see **zero** — a bigger and far more frequent
complaint than the truncation this migration exists to prevent, because it hits
every active user every day. Real-time aggregation unions the materialised rows
with a live scan of the un-materialised tail, so the current hour is correct.
The setting cannot be folded into the ``CREATE``: the ``WITH`` clause of a
continuous aggregate rejects it, so it is a separate ``ALTER``.

The comment is attached with ``COMMENT ON VIEW``, not
``COMMENT ON MATERIALIZED VIEW``. Despite the ``CREATE MATERIALIZED VIEW``
spelling, a continuous aggregate is a plain view (``pg_class.relkind = 'v'``)
over a hidden materialisation hypertable, and the materialized form is rejected
with ``"..." is not a materialized view``.

**start_offset (30 days) and the retention window (13 months) must agree.**
They are stated adjacent here so the relationship is visible when either one
changes. Refreshing a window whose raw chunks have already been dropped does
**not** error — it recomputes that window as empty and DELETES the materialised
rows for it. 30 days is comfortably inside 13 months, so the scheduled policy
can never reach into dropped chunks. The same hazard is why the backfill CLI
refuses a window starting before the retention boundary without ``--force``.

Created ``WITH NO DATA``, following ``i9j0k1l2m3n4``. A full-history
materialisation cannot run here: ``refresh_continuous_aggregate`` cannot run
inside a transaction block, and ``entrypoint.sh`` runs ``flask db upgrade``
before ``exec uvicorn``, so an unbounded refresh over a production-sized
hypertable would block container start for as long as it took. The backfill is
a deliberate operator command instead.

No retention policy is created here, or in any migration. See ``i3j4k5l6m7n8``.

"""

from alembic import op

revision = "g1h2i3j4k5l6"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None

VIEW = "request_counts_hourly_by_entity"

# Cumulative upper edges, in seconds, for the ttft_visible histogram. The column
# suffix has '.' replaced by '_' so it is a bare identifier.
TTFT_EDGES = [("0_5", "0.5"), ("1", "1"), ("2", "2"), ("5", "5"), ("10", "10"), ("30", "30")]

_TTFT_BUCKETS = ",\n                ".join(
    f"COUNT(*) FILTER (WHERE ttft_visible <= {edge}) AS ttft_le_{suffix}"
    for suffix, edge in TTFT_EDGES
)

_COMMENT = (
    "Hourly per-entity usage rollup of request_logs, grouped by "
    "(bucket, entity_id, model_config_id, source). Answers \"what did this "
    "user do, and how did it feel\" - request/token/cost totals, duration and "
    "time-to-first-token sums and maxima, abort counts, and cumulative "
    "ttft_visible histogram buckets. It exists so the per-entity /usage charts "
    "survive retention dropping raw chunks; request_counts_hourly has no "
    "entity_id and cannot answer for an individual. Real-time "
    "(materialized_only = false), so the current hour is included."
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if not _is_postgresql():
        # SQLite has no continuous aggregates; the /usage endpoints that read
        # this view already short-circuit on the dialect check before querying.
        return

    op.execute(f"""
        CREATE MATERIALIZED VIEW {VIEW}
        WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('1 hour', time) AS bucket,
                entity_id,
                model_config_id,
                source,
                COUNT(*)           AS requests,
                SUM(input_tokens)  AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost)          AS cost,
                SUM(duration)      AS duration_sum,
                MAX(duration)      AS duration_max,
                COUNT(*) FILTER (WHERE outcome = 'disconnect') AS aborts,
                COUNT(ttft_visible) AS ttft_count,
                SUM(ttft_visible)   AS ttft_sum,
                MAX(ttft_visible)   AS ttft_max,
                {_TTFT_BUCKETS}
            FROM request_logs
            GROUP BY 1, 2, 3, 4
        WITH NO DATA
    """)

    # Separate statement: the CREATE's WITH clause does not accept this, and
    # leaving it at its 2.27 default (true) hides the current bucket entirely.
    op.execute(
        f"ALTER MATERIALIZED VIEW {VIEW} SET (timescaledb.materialized_only = false)"
    )

    # start_offset 30 days vs. the 13-month retention window — see the docstring.
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{VIEW}',
            start_offset => INTERVAL '30 days',
            end_offset   => INTERVAL '1 hour',
            schedule_interval => INTERVAL '1 hour')
    """)

    op.execute(f"COMMENT ON VIEW {VIEW} IS '{_COMMENT}'")


def downgrade():
    if not _is_postgresql():
        return

    # The policy has to go first: a job referencing the view blocks the DROP.
    op.execute(f"SELECT remove_continuous_aggregate_policy('{VIEW}', if_exists => true)")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW}")
