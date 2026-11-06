"""Switch the request_logs foreign keys to ON DELETE RESTRICT

Revision ID: c8d0e2f4a6b8
Revises: b4c6d8e0f2a4
Create Date: 2026-11-01 00:00:00.000000

``request_logs.entity_id``, ``model_config_id`` and ``model_endpoint_id`` were
``ON DELETE SET NULL``. On the compressed hypertable that action is a hazard,
not a safety net: deleting one parent row runs ``UPDATE request_logs SET
<column> = NULL`` over every chunk. TimescaleDB has to decompress each
compressed batch that may hold the id, and past
``timescaledb.max_tuples_decompressed_per_dml_transaction`` (100,000 by default)
the statement aborts. When it does not abort, it rewrites history: the rows lose
who made them, and the touched chunks are left partly uncompressed.

With ``RESTRICT`` a direct ``DELETE`` of a referenced row fails instead.
Entities are deactivated, models and endpoints are retired; none is deleted
while it has request history. The only sanctioned hard delete of an entity is
``flask erase-entity`` (#378), which first moves the entity's ``request_logs``
rows to the ghost entity. ``RESTRICT`` is its safety net: if the move missed a row,
the final ``DELETE`` fails rather than leaving a row that belongs to nobody.

What a direct ``DELETE`` costs now. The check only reads ``request_logs``; it
never writes a row or decompresses a chunk on disk.

- Model: an index lookup per chunk. ``model_config_id`` leads
  ``ix_request_logs_model_config_id_time`` on uncompressed chunks and is the
  compression ``segmentby`` on compressed ones.
- Entity: an index lookup on uncompressed chunks
  (``ix_request_logs_entity_id``), but compressed chunks have no ``entity_id``
  index, so they are decompressed in memory and scanned.
- Endpoint: ``model_endpoint_id`` has no index at all, so every chunk is
  scanned, compressed ones decompressed in memory.

The entity and endpoint cases are slow and then fail. No index is added for a
path that should never run.

Deploy lock. PostgreSQL cannot change an FK's delete action in place, so each
constraint is dropped and re-added under the same name. A plain
``ADD FOREIGN KEY`` would validate every existing row, compressed chunks
included, while holding a ``SHARE ROW EXCLUSIVE`` lock on ``request_logs`` and
its chunks. That lock blocks inserts, so billing would wait for three full
scans of the never-pruned hypertable, one per constraint.

Each constraint is therefore re-added ``NOT VALID``, in both directions, like
``fk_request_logs_api_key_id`` in ``c5d7e9f1a3b6``. That is a catalogue-only
change: new INSERTs and UPDATEs are checked from now on, existing rows are not
scanned, and the delete action applies at once. Skipping the check loses
nothing, because the constraint just dropped covered the same column and the
same parent and had already proved every existing row.

There is no ``VALIDATE CONSTRAINT``, here or later, so ``convalidated`` stays
``false`` on these three FKs on purpose. On its own ``VALIDATE`` takes only
``SHARE UPDATE EXCLUSIVE``, but ``env.py`` runs every pending revision in one
transaction. It would run after ``DROP CONSTRAINT`` has taken
``ACCESS EXCLUSIVE`` on ``request_logs``, a lock held until commit, so billing
would still wait for the full scans, which would prove nothing new.

The column comments are updated to the new stance; the downgrade restores
``SET NULL`` and the previous comments.

SQLite is skipped. The app never enables ``PRAGMA foreign_keys``, so SQLite
enforces neither action, and changing an FK there means rebuilding the whole
table with ``batch_alter_table``. A fresh ``create_all`` gets ``RESTRICT`` from
the model.
"""

import sqlalchemy as sa
from alembic import op

revision = "c8d0e2f4a6b8"
down_revision = "b4c6d8e0f2a4"
branch_labels = None
depends_on = None

# (constraint, column, parent table, comment after upgrade, comment before upgrade)
_FKS = [
    (
        "request_logs_entity_id_fkey",
        "entity_id",
        "entities",
        "Requesting entity; RESTRICT on delete: entities are deactivated, not deleted, while they have request history",
        "Requesting entity; SET NULL on delete to preserve historical data",
    ),
    (
        "request_logs_model_config_id_fkey",
        "model_config_id",
        "model_configs",
        "Model used; RESTRICT on delete: models are retired (deactivated), never deleted",
        "Model used; SET NULL on delete to preserve historical data",
    ),
    (
        "request_logs_model_endpoint_id_fkey",
        "model_endpoint_id",
        "model_endpoints",
        "Backend endpoint that served the request; RESTRICT on delete: endpoints are retired (deactivated), never deleted",
        "Backend endpoint that served the request; endpoints are retired (deactivated), never deleted, so this reference is stable",
    ),
]


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _set_comment(column, comment):
    op.execute(sa.text(f"COMMENT ON COLUMN request_logs.{column} IS $comment${comment}$comment$"))


def _switch(restrict):
    for name, column, parent, restrict_comment, set_null_comment in _FKS:
        op.drop_constraint(name, "request_logs", type_="foreignkey")
        # NOT VALID: see "Deploy lock" in the module docstring. Never followed
        # by VALIDATE CONSTRAINT.
        op.execute(
            f"ALTER TABLE request_logs ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({column}) REFERENCES {parent} (id) "
            f"ON DELETE {'RESTRICT' if restrict else 'SET NULL'} NOT VALID"
        )
        _set_comment(column, restrict_comment if restrict else set_null_comment)


def upgrade():
    if _is_postgresql():
        _switch(restrict=True)


def downgrade():
    if _is_postgresql():
        _switch(restrict=False)
