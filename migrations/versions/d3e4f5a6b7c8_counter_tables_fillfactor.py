"""Set fillfactor = 80 on the per-request counter tables

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-28 00:00:00.000000

Every billed request updates a row in ``entity_balances``, ``entity_stats``,
``model_stats`` and ``api_keys``. Leaving 20% of each page free lets
PostgreSQL keep those updates HOT (heap-only tuple, no index writes) on the
same page. ``ALTER TABLE ... SET (fillfactor)`` only changes the storage
parameter: existing pages fill to the new limit as they are rewritten by
vacuum or updates, so there is no table rewrite and no long lock.
PostgreSQL only; a no-op on SQLite.
"""

from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None

_TABLES = ("entity_balances", "entity_stats", "model_stats", "api_keys")


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} SET (fillfactor = 80)")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} RESET (fillfactor)")
