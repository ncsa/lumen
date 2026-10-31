"""Add folded_* columns to model_stats and entity_stats

Revision ID: a6b8c0d2e4f6
Revises: f5b6c7d8e9f0
Create Date: 2026-10-30 00:00:00.000000

The running totals mix history from before ``request_logs`` existed, the
per-request bumps from before the stats cutover, and what the fold has added
since. ``flask check-stats`` compares the fold's part with ``request_logs``
since ``cutover_at``, so the fold records that part on its own, next to the
totals it adds it to.
"""

import sqlalchemy as sa
from alembic import op

revision = "a6b8c0d2e4f6"
down_revision = "f5b6c7d8e9f0"
branch_labels = None
depends_on = None

#: (table, comment before, comment after): the totals are now folded.
_TABLES = (
    ("model_stats",
     "Aggregated usage counters per (entity, model, source) triple; updated on every proxied request",
     "Aggregated usage counters per (entity, model, source) triple; folded from request_logs (bumped per request before the stats cutover)"),
    ("entity_stats",
     "Aggregated usage totals per entity across all models and sources; updated on every proxied request",
     "Aggregated usage totals per entity across all models and sources; folded from request_logs (bumped per request before the stats cutover)"),
)
_COLUMNS = (
    ("folded_requests", sa.Integer(), "Part of requests added by the stats fold since the cutover; check-stats compares it with request_logs"),
    ("folded_input_tokens", sa.BigInteger(), "Part of input_tokens added by the stats fold since the cutover"),
    ("folded_output_tokens", sa.BigInteger(), "Part of output_tokens added by the stats fold since the cutover"),
    ("folded_audio_seconds", sa.BigInteger(), "Part of audio_seconds added by the stats fold since the cutover"),
    ("folded_cost", sa.Numeric(12, 6), "Part of cost added by the stats fold since the cutover"),
)


def upgrade():
    postgresql = op.get_bind().dialect.name == "postgresql"
    for table, before, after in _TABLES:
        with op.batch_alter_table(table) as batch:
            for name, type_, comment in _COLUMNS:
                batch.add_column(sa.Column(name, type_, nullable=False, server_default="0", comment=comment))
        if postgresql:
            op.create_table_comment(table, after, existing_comment=before)


def downgrade():
    postgresql = op.get_bind().dialect.name == "postgresql"
    for table, before, after in _TABLES:
        if postgresql:
            op.create_table_comment(table, before, existing_comment=after)
        with op.batch_alter_table(table) as batch:
            for name, _, _ in reversed(_COLUMNS):
                batch.drop_column(name)
