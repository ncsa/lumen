"""Add the request_metrics_1m continuous aggregate

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-08-18 00:00:00.000000

The hourly aggregates answer "what did this term look like". Neither can answer
"what is happening right now", which is the only question that matters while a
class start is going wrong. An hourly bucket with ``end_offset => 1 hour``
cannot resolve a ten-minute burst at all: the incident is over before the bucket
it lives in is even closed.

So this view buckets by the minute, with ``end_offset => 1 minute`` and a
matching one-minute schedule, over ``(bucket, model_config_id, source)``. It
carries the same measures as ``request_counts_hourly_by_entity`` — counts,
tokens, cost, duration sum/max, aborts, and the cumulative ``ttft_le_*``
histogram — because the operator questions during a burst are the same ones
(*is TTFT climbing? are people hanging up?*), just at a resolution the hourly
view cannot produce.

**Deliberately no ``entity_id``.** This view answers "what happened during the
9 a.m. lab", not "what did this student do" — the latter is what
``request_counts_hourly_by_entity`` is for. Adding ``entity_id`` here would
multiply the row count by the size of the class *in every minute*, for no
requirement anything states. A 300-student burst produces a few hundred rows per
minute with the entity dimension and a handful without it.

``materialized_only`` is set to false for the same reason as the hourly view,
and it matters more here: with a one-minute ``end_offset``, a
materialised-only view is by construction always at least a minute stale, which
is most of the resolution this view exists to provide. Real-time aggregation
scans the raw tail for the last minute or two, which is a trivially small range
on a hypertable partitioned by time. As in ``g1h2i3j4k5l6``, the comment is
attached with ``COMMENT ON VIEW``: a continuous aggregate is a plain view
whatever the ``CREATE MATERIALIZED VIEW`` spelling suggests.

**start_offset is 3 hours, against a 13-month retention window** — the two
numbers are stated adjacent, as in ``g1h2i3j4k5l6``, because a refresh reaching
into dropped chunks recomputes the window as empty and deletes the materialised
rows without erroring. 3 hours is not a storage decision: it is the refresh
*window*, chosen so each scheduled run rewrites only recent minutes rather than
re-scanning a day. Once a minute has been materialised the policy never revisits
it, so in steady state every minute since the view was created is held.

Be precise about the one gap that leaves: minutes that predate this migration
were never materialised, and once the first policy run advances the watermark
they are *invisible* through the view rather than merely absent, because
real-time aggregation only scans raw rows above the watermark. That is the same
behaviour that forces a backfill for the hourly per-entity view. It is harmless
here and only here, because this view answers "what is happening right now" and
nothing asks it about last March -- but it is a property of the view, not an
accident, and a future caller that asks it a historical question will get a
confident wrong answer rather than an error.

Created ``WITH NO DATA``, for the reason given in ``g1h2i3j4k5l6``: the refresh
cannot run inside a transaction block and ``entrypoint.sh`` runs
``flask db upgrade`` on the container start path. Unlike the hourly view, this
one needs no historical backfill — nothing asks it about last March — so the
policy filling in its 3-hour window is all it ever needs.

"""

from alembic import op

revision = "h2i3j4k5l6m7"
down_revision = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None

VIEW = "request_metrics_1m"

# Same cumulative edges as the hourly view, so p95 is computed identically from
# either. Kept as a literal here rather than imported across revisions: a
# migration must keep meaning what it meant on the day it ran.
TTFT_EDGES = [("0_5", "0.5"), ("1", "1"), ("2", "2"), ("5", "5"), ("10", "10"), ("30", "30")]

_TTFT_BUCKETS = ",\n                ".join(
    f"COUNT(*) FILTER (WHERE ttft_visible <= {edge}) AS ttft_le_{suffix}"
    for suffix, edge in TTFT_EDGES
)

_COMMENT = (
    "One-minute rollup of request_logs, grouped by (bucket, model_config_id, "
    "source). Answers \"what is happening right now, per model\" at a "
    "resolution the hourly aggregates cannot reach - a ten-minute burst is "
    "over before an hourly bucket closes. Carries request/token/cost totals, "
    "duration and time-to-first-token sums and maxima, abort counts, and "
    "cumulative ttft_visible histogram buckets. No entity_id on purpose: this "
    "is the shape of the load, not who caused it. Real-time "
    "(materialized_only = false), so the current minute is included."
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
                time_bucket('1 minute', time) AS bucket,
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
            GROUP BY 1, 2, 3
        WITH NO DATA
    """)

    op.execute(
        f"ALTER MATERIALIZED VIEW {VIEW} SET (timescaledb.materialized_only = false)"
    )

    # start_offset 3 hours vs. the 13-month retention window — see the docstring.
    op.execute(f"""
        SELECT add_continuous_aggregate_policy('{VIEW}',
            start_offset => INTERVAL '3 hours',
            end_offset   => INTERVAL '1 minute',
            schedule_interval => INTERVAL '1 minute')
    """)

    op.execute(f"COMMENT ON VIEW {VIEW} IS '{_COMMENT}'")


def downgrade():
    if not _is_postgresql():
        return

    op.execute(f"SELECT remove_continuous_aggregate_policy('{VIEW}', if_exists => true)")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW}")
