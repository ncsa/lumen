"""Add per-key cached-input and cache-write token totals to api_keys

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-11-10 00:00:00.000000

api_keys already keeps running totals of requests, tokens and cost so
GET /v1/usage can report a key's usage without scanning request_logs. The
cached-input and cache-write counts recorded per request since d2e3f4a5b6c7 and
e3f4a5b6c7d8 had no per-key total, so this adds one for each:

* api_keys.cached_input_tokens — part of input_tokens the upstream served
  from its prompt cache.
* api_keys.cache_write_tokens — part of input_tokens the upstream wrote into
  its prompt cache.

Both are counters (NOT NULL, default 0) and are backfilled from each key's
request_logs rows, where a NULL count adds nothing, so the totals match the log
from the start. bill_request adds every later request to them.

request_logs.api_key_id has no index (see c5d7e9f1a3b6), so the backfill
aggregates the history once, grouped by key, and joins that result to
api_keys; a per-key subquery would scan the whole hypertable once per key while
the api_keys DDL lock is held. Keys with no logged requests keep the default 0.
"""

import sqlalchemy as sa
from alembic import op

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None

_COMMENTS = {
    "cached_input_tokens": (
        "Cumulative cached-input part of input_tokens via this key (NULL request_logs counts add nothing)"
    ),
    "cache_write_tokens": (
        "Cumulative cache-write part of input_tokens via this key (NULL request_logs counts add nothing)"
    ),
}


# One pass over request_logs, grouped by key. UPDATE ... FROM works on
# PostgreSQL and on SQLite >= 3.33. A group whose counts are all NULL sums to
# NULL, which COALESCE turns into 0.
BACKFILL_SQL = (
    "UPDATE api_keys SET "
    "cached_input_tokens = totals.cached_input_tokens, "
    "cache_write_tokens = totals.cache_write_tokens "
    "FROM (SELECT api_key_id, "
    "COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens, "
    "COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens "
    "FROM request_logs WHERE api_key_id IS NOT NULL GROUP BY api_key_id) AS totals "
    "WHERE api_keys.id = totals.api_key_id"
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        for column in _COMMENTS:
            batch_op.add_column(sa.Column(column, sa.BigInteger(), nullable=False, server_default="0"))

    if _is_postgresql():
        for column, comment in _COMMENTS.items():
            op.execute(f"COMMENT ON COLUMN api_keys.{column} IS $comment${comment}$comment$")

    op.execute(BACKFILL_SQL)


def downgrade():
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("cache_write_tokens")
        batch_op.drop_column("cached_input_tokens")
