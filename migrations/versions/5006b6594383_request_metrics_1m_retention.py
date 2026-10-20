"""Keep 30 days of request_metrics_1m

Revision ID: 5006b6594383
Revises: c93d4de8245c
Create Date: 2026-10-20 00:00:00.000000

``request_metrics_1m`` answers "what is happening right now, per model" (#169):
``/v1/models`` and the model detail page read their last-minute, last-hour and
last-day request counts from it. Without retention it keeps a row per model and
source for every minute since it was created, forever, for a question that never
looks back further than a day. This drops its materialised buckets after 30 days,
which is enough for any "right now" question and leaves room for dashboards.

**The policy is on the aggregate, never on ``request_logs``.** ``request_logs``
is never pruned (#167): it is the only source the aggregates can be rebuilt from.
Retention on a continuous aggregate drops chunks of its materialisation
hypertable only, so the raw rows the dropped buckets were computed from stay
where they are. ``_retention_job()``, which backs ``flask check-aggregate`` and
the startup tripwire, matches only a policy on ``request_logs`` and so does not
fire on this one.

The view name is spelled as a string literal on purpose:
``tests/unit/test_request_logs_never_pruned.py`` allows ``add_retention_policy``
only when its target is the literal ``'request_metrics_1m'``, because it cannot
prove what a variable or an f-string points at.

**drop_after is 30 days, against a 3-hour refresh start_offset** — the two
numbers are stated adjacent, as in ``h2i3j4k5l6m7``, because a refresh whose
window reached into dropped buckets would rematerialise them from raw on every
run. The scheduled refresh only ever rewrites the last 3 hours, so it never
reaches a bucket older than 30 days. Nothing reads further back than 24 hours,
so no reader ever asks for a dropped bucket either.

PostgreSQL only, like the view itself.
"""

from alembic import op

revision = "5006b6594383"
down_revision = "c93d4de8245c"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if not _is_postgresql():
        return

    # drop_after 30 days vs. the 3-hour refresh start_offset, and no reader
    # looking back more than 24 hours — see the docstring.
    op.execute("""
        SELECT add_retention_policy('request_metrics_1m',
            drop_after => INTERVAL '30 days')
    """)


def downgrade():
    if not _is_postgresql():
        return

    op.execute("SELECT remove_retention_policy('request_metrics_1m', if_exists => true)")
