"""Add per-request timing columns to request_logs

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-18 00:00:00.000000

``request_logs`` has had exactly one timing column, ``duration``, and it does
not mean what an operator assumes. It starts *after* the model lookup, endpoint
selection and coin-budget checks have already run, and it ends before the
billing commit. ``time`` is stamped after that commit. So the row records how
long the upstream call took, and says nothing about how long the *user* waited.

That gap is why a class-start incident is currently undiagnosable: "queued
behind other students for a worker thread", "waiting on a contended connection
pool", and "the model itself is slow" all produce the same row.

Seven columns, all riding the INSERT that already happens in ``update_stats``,
so the hot path gains no statement:

  started_at    absolute arrival at the ASGI bridge (T0)
  queue_wait    T1-T0, waiting for a WSGI worker thread
  preflight     T2-T1, auth/lookup/budget/pool checkout
  ttft          first upstream chunk of any kind
  ttft_visible  first visible content delta
  send_blocked  time blocked handing chunks to the server
  outcome       ok | disconnect

``started_at`` is stored rather than derived. The obvious reconstruction,
``time - duration - queue_wait``, is wrong: it silently assumes preflight and
the billing commit take zero time, and both are largest exactly during the
burst the column exists to explain, so the error is worst when the data matters
most.

``started_at`` is TIMESTAMPTZ, unlike every other timestamp column in the
schema (naive UTC, per CLAUDE.md). It exists to be compared and subtracted
against ``time`` on the same row, and mixing naive with aware in that
arithmetic is a Postgres footgun. It must be written with
``datetime.now(timezone.utc)``.

``outcome`` carries only the two values the code can actually write. In
particular there is deliberately no ``billing_error``: the row is created by
``update_stats``, which only flushes, so a failing commit rolls back the very
row that would have recorded the failure. A value that can never be written
would make the column disagree with the abort counter beside it. Add values
together with the code that writes them, never ahead of it.

No backfill, following ``e6f7a8b9c0d1``. Historical rows have no arrival time
to recover — nothing recorded one — and inventing one from ``time - duration``
would produce a column that looks authoritative and is quietly wrong for every
pre-migration row. NULL means "not measured", which is honest and easy to
filter.

**Every column is added bare — nullable, no server default — and that is the
load-bearing detail.** ``ALTER TABLE ... ADD COLUMN x FLOAT DEFAULT 0`` does not
merely default *future* inserts: PostgreSQL initialises **every existing row** to
0 as part of the ADD. On thirteen months of ``request_logs`` a default of 0 would
therefore write "measured, and instant" over the entire history — and the
aggregates read exactly those columns. ``ttft_count`` is ``COUNT(ttft_visible)``,
so it would count every pre-migration row; every cumulative ``ttft_le_*`` bucket
filters ``ttft_visible <= edge``, so it would count them all as sub-edge; and p95
TTFT for every historical bucket would read 0 seconds. Once retention drops the
raw chunks, only those materialised zeros remain. NULL is the only value that
tells the truth about a row that predates the measurement, and it is also what a
live request that never passed through the ASGI bridge (dev server, test client)
must store.

The Timescale restriction sometimes cited as requiring the default is a
different one: a **NOT NULL** column with no default is what a populated
hypertable rejects (see ``y9z0a1b2c3d4``). These columns are nullable, so it does
not apply — a bare nullable ADD COLUMN propagates to every existing chunk,
including compressed ones, which
``test_add_column_still_works_on_a_compressed_hypertable`` asserts on the
deployed 2.27.2.

"""

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


# (name, type) — nullable and without a server default in every case.
_COLUMNS = [
    ("started_at", sa.DateTime(timezone=True)),
    ("queue_wait", sa.Float()),
    ("preflight", sa.Float()),
    ("ttft", sa.Float()),
    ("ttft_visible", sa.Float()),
    ("send_blocked", sa.Float()),
    ("outcome", sa.String(16)),
]


def upgrade():
    # Nullable, and deliberately WITHOUT a server default. ADD COLUMN ... DEFAULT
    # backfills every existing row with that default, so a default of 0 would
    # record thirteen months of history as "measured, and instant" (see the
    # module docstring). Only a NOT NULL column needs a default for a populated
    # hypertable to accept it (y9z0a1b2c3d4); these are nullable.
    with op.batch_alter_table("request_logs") as batch_op:
        for name, type_ in _COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))

    if _is_postgresql():
        # The operator queries all filter by model and time; this is the
        # composite they need, and it is absent today.
        op.create_index(
            "ix_request_logs_model_config_id_time",
            "request_logs",
            ["model_config_id", sa.text("time DESC")],
        )
        for name, comment in [
            ("started_at", "UTC instant the request arrived at the ASGI bridge (T0). TIMESTAMPTZ to match `time` for same-row arithmetic; null when the request did not pass through the bridge"),
            ("queue_wait", "Seconds waiting for a WSGI worker thread (T1-T0)"),
            ("preflight", "Seconds from worker pickup to the upstream call (T2-T1). Composition differs by path: audio is dominated by upload parsing, the chat stream spans two preflights"),
            ("ttft", "Seconds to the first upstream chunk of any kind, including reasoning deltas"),
            ("ttft_visible", "Seconds to the first visible content delta; trails ttft by the thinking phase on a reasoning model"),
            ("send_blocked", "Seconds blocked handing chunks to the server; a large share means a slow client, not a slow backend"),
            ("outcome", "How the request ended: ok | disconnect. Null means the row predates this column"),
        ]:
            op.execute(
                f"COMMENT ON COLUMN request_logs.{name} IS '{comment}'"
            )


def downgrade():
    if _is_postgresql():
        op.drop_index("ix_request_logs_model_config_id_time", table_name="request_logs")
    with op.batch_alter_table("request_logs") as batch_op:
        for name, _type in reversed(_COLUMNS):
            batch_op.drop_column(name)
