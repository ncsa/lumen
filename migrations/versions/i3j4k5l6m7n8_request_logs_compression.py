"""Enable compression on request_logs, with a 7-day policy

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-08-18 00:00:00.000000

``request_logs`` is append-only and its rows are extremely compressible: within
one chunk, ``model_config_id`` and ``source`` take a handful of distinct values
and ``time`` is nearly sorted. Segmenting by ``(model_config_id, source)`` and
ordering by ``time DESC`` is therefore the natural layout — it is also the exact
shape the analytics queries filter on, so a compressed chunk can be pruned by
segment without decompressing it.

The policy compresses chunks older than 7 days, which is the chunk interval
``i9j0k1l2m3n4`` created, so a chunk becomes eligible only once it is closed and
no longer being written. That alignment is the point: compressing an open chunk
would mean recompressing it on every insert.

**This migration must run after both continuous aggregates, and the ordering is
load-bearing.** The aggregates read raw ``request_logs``; compression changes
how those reads execute but not what they return, and creating an aggregate over
an already-compressed hypertable is the slower path. More importantly, this
migration is the last reversible step of the lifecycle work, and reversing it
after retention had already dropped chunks would restore nothing.

**Compression is not a one-way door on this deployment, and that was measured
rather than assumed.** On ``timescale/timescaledb:2.27.2-pg17``, against a
hypertable with genuinely compressed chunks, nullable ``ADD COLUMN``,
``ADD COLUMN ... DEFAULT`` and ``ADD COLUMN ... NOT NULL DEFAULT`` all succeed.
Only ``NOT NULL`` *without* a default is refused, and it is refused loudly
(``cannot add column with NOT NULL constraint without default to a hypertable
that has columnstore enabled``) — the same restriction ``y9z0a1b2c3d4``
documents, restated in 2.27's columnstore vocabulary. So later phases may add
nullable columns freely. The residual cost is I/O, not capability: servicing an
``ADD COLUMN`` still rewrites compressed chunks across the whole window, which
is a reason to prefer settling schema first, not a reason to defer this.

**No retention policy is created here, or anywhere else in the migration
chain.** ``entrypoint.sh`` runs ``flask db upgrade`` at container start, so a
migration calling ``add_retention_policy`` would begin deleting production data
automatically on the next deploy — which makes the phase's exit gate ("retention
is enabled only after a dry run on a copy of production shows no per-user chart
changes") unenforceable by construction. Compression stays a migration precisely
because it is the opposite: it destroys nothing and ``downgrade()`` genuinely
puts the table back. Retention is a deliberate operator command instead.

``downgrade()`` decompresses every chunk before turning compression off, because
disabling it on a hypertable that still has compressed chunks is rejected. That
makes the downgrade proportional to the data — it is a real rewrite, not a
catalogue edit — which is expected and is the honest cost of reversing this.

"""

from alembic import op

revision = "i3j4k5l6m7n8"
down_revision = "h2i3j4k5l6m7"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if not _is_postgresql():
        # SQLite stores request_logs as a plain table; there is nothing to compress.
        return

    op.execute("""
        ALTER TABLE request_logs SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'model_config_id, source',
            timescaledb.compress_orderby = 'time DESC'
        )
    """)
    # 7 days == the chunk interval, so only closed chunks are ever compressed.
    op.execute("SELECT add_compression_policy('request_logs', INTERVAL '7 days')")


def downgrade():
    if not _is_postgresql():
        return

    op.execute("SELECT remove_compression_policy('request_logs', if_exists => true)")
    # Every chunk must be uncompressed before compression can be disabled.
    op.execute("SELECT decompress_chunk(c, true) FROM show_chunks('request_logs') c")
    op.execute("ALTER TABLE request_logs SET (timescaledb.compress = false)")
