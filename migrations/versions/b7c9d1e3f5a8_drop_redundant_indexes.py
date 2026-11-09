"""Drop redundant indexes on request_logs and model_stats

Revision ID: b7c9d1e3f5a8
Revises: a6b8c0d2e4f6
Create Date: 2026-10-31 00:00:00.000000

Three indexes duplicate another index on the same table. Every insert pays for
them and no query needs them:

- ``ix_request_logs_time`` (``time``) duplicates ``request_logs_time_idx``
  (``time DESC``), which TimescaleDB's ``create_hypertable`` creates by default.
  A B-tree is scanned in both directions, so the two serve the same queries.
- ``ix_request_logs_model_config_id`` (``model_config_id``) is the leading
  column of ``ix_request_logs_model_config_id_time`` (``f7a8b9c0d1e2``), which
  serves every lookup by model alone.
- ``ix_model_stats_entity_id`` (``entity_id``) is the leading column of
  ``UNIQUE(entity_id, model_config_id, source)``.

``request_logs_time_idx`` is now declared on the model, so autogenerate and a
fresh ``create_all`` agree with an upgraded schema.

On SQLite, where the hypertable index does not exist, ``ix_request_logs_time``
and ``ix_request_logs_model_config_id`` were created too (``y9z0a1b2c3d4``,
``z0a1b2c3d4e5``). The upgrade drops them there as well and creates
``request_logs_time_idx`` so the schema matches the model on both dialects.
The composite ``ix_request_logs_model_config_id_time`` is PostgreSQL-only in the
migration chain and is left alone.

Locks: ``DROP INDEX`` on a hypertable drops the index on every chunk, each
under a brief exclusive lock. The downgrade's ``CREATE INDEX`` builds the index
over every chunk and blocks writes to ``request_logs`` while it runs;
TimescaleDB does not support ``CONCURRENTLY`` on hypertables.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c9d1e3f5a8"
down_revision = "a6b8c0d2e4f6"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    op.drop_index("ix_request_logs_time", table_name="request_logs", if_exists=True)
    op.drop_index("ix_request_logs_model_config_id", table_name="request_logs", if_exists=True)
    if not _is_postgresql():
        # On PostgreSQL TimescaleDB created this with the hypertable.
        op.create_index("request_logs_time_idx", "request_logs", [sa.text("time DESC")], if_not_exists=True)
    op.drop_index("ix_model_stats_entity_id", table_name="model_stats", if_exists=True)


def downgrade():
    op.create_index("ix_model_stats_entity_id", "model_stats", ["entity_id"])
    if not _is_postgresql():
        op.drop_index("request_logs_time_idx", table_name="request_logs")
    op.create_index("ix_request_logs_model_config_id", "request_logs", ["model_config_id"])
    op.create_index("ix_request_logs_time", "request_logs", ["time"])
