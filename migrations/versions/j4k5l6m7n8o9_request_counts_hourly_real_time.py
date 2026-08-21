"""Make request_counts_hourly a real-time aggregate

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-08-18 00:00:00.000000

``request_counts_hourly`` was created by ``i9j0k1l2m3n4`` without ever setting
``timescaledb.materialized_only``. That was correct on the TimescaleDB of the
day — real-time aggregation was the default — and it stopped being correct in
**2.13, which flipped the default to ``true``**. The deployed image is 2.27.2,
so the view has been materialised-only in production ever since that upgrade,
silently and with nothing in the migration chain recording the change.

**The user-visible symptom.** With ``materialized_only = true`` a query against
the view reads *only* the materialisation hypertable, and the refresh policy
(``end_offset => 1 hour``, ``schedule_interval => 1 hour``) never materialises
anything newer than an hour ago — and only once an hour, so in the worst case
the newest bucket present is nearly two hours old. Every **org-wide** chart on
``/usage`` reads this view and nothing else: there is no raw-``request_logs``
fallback on that path, unlike the per-entity charts. So the org-wide totals,
the model breakdown and the activity heatmap have all been missing up to the
last two hours of traffic, permanently. It reads as "the numbers are behind"
rather than as an error, which is why it survived: nothing is ever missing from
a *finished* day, only from the day being looked at.

**Why real-time aggregation is the fix.** With ``materialized_only = false``
a query unions the materialised rows below the watermark with a live scan of
the raw rows above it. The materialised half is unchanged; the tail — the part
the policy has not reached yet — is read straight from ``request_logs``, which
is where those rows already are. The result is the whole window, with the
current bucket correct at every instant, and the cost is a scan of at most the
last two hours of one hypertable partitioned by time. This is the same setting
``g1h2i3j4k5l6`` and ``h2i3j4k5l6m7`` set explicitly on the two aggregates
added after the default flipped; this migration brings the one that predates
them into line, so all three views answer the same question the same way.

**``end_offset`` is deliberately left at 1 hour.** It is tempting to shrink it
now that the gap is visible, and it would make things worse. ``end_offset`` is
where the policy stops materialising; real-time aggregation already covers
everything past that point, so lowering it buys no freshness at all. What it
would buy is a *partial* current bucket written below the watermark: once the
watermark passes the open bucket, real-time aggregation no longer scans raw
rows for it, and every request made in the rest of that hour is invisible until
the next scheduled run rewrites it. That is exactly the failure
``test_backfill_leaves_real_time_aggregation_working_for_the_current_bucket``
pins down for the backfill command, and an ``end_offset`` shorter than the
bucket width would reintroduce it on a schedule. One hour — one bucket width —
keeps the watermark on a closed bucket, which is the precondition real-time
aggregation needs. It also matches ``request_counts_hourly_by_entity``, which
has the same bucket and the same ``end_offset``.

No data is rewritten and nothing is recomputed: this is a catalogue setting, so
both directions are instant and ``downgrade()`` genuinely restores the previous
behaviour by setting it back to ``true`` — the value the view has been running
with, rather than an implicit default that changes meaning across versions.

"""

from alembic import op

revision = "j4k5l6m7n8o9"
down_revision = "i3j4k5l6m7n8"
branch_labels = None
depends_on = None

VIEW = "request_counts_hourly"

_COMMENT = (
    "Hourly org-wide rollup of request_logs, grouped by (bucket, "
    "model_config_id, source), carrying request/token/cost totals. No "
    "entity_id: the per-entity /usage charts read "
    "request_counts_hourly_by_entity instead. Real-time "
    "(materialized_only = false), so the current hour is included."
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if not _is_postgresql():
        # SQLite has no continuous aggregates; i9j0k1l2m3n4 created a plain
        # table there and never created this view.
        return

    op.execute(
        f"ALTER MATERIALIZED VIEW {VIEW} SET (timescaledb.materialized_only = false)"
    )
    # A continuous aggregate is a plain view (pg_class.relkind = 'v') whatever
    # the CREATE MATERIALIZED VIEW spelling suggests, so COMMENT ON VIEW is the
    # form that is accepted — see g1h2i3j4k5l6.
    op.execute(f"COMMENT ON VIEW {VIEW} IS '{_COMMENT}'")


def downgrade():
    if not _is_postgresql():
        return

    op.execute(
        f"ALTER MATERIALIZED VIEW {VIEW} SET (timescaledb.materialized_only = true)"
    )
    op.execute(f"COMMENT ON VIEW {VIEW} IS NULL")
