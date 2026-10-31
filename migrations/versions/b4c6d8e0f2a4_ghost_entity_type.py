"""Add the 'ghost' entity type and a unique index allowing a single ghost row

Revision ID: b4c6d8e0f2a4
Revises: d3e4f5a6b7c8
Create Date: 2026-10-31 00:00:00.000000

GDPR erasure (#378) moves an erased user's request history, stats, models and
revoked API keys to one shared, hidden, inactive entity of type 'ghost'.
  * PostgreSQL: ck_entities_type accepts 'ghost'; the entities.entity_type
    comment lists it, and the api_keys.key_hash comment says the hash is a
    keyed HMAC rather than a plain SHA-256.
  * Both dialects: the partial unique index uq_entities_single_ghost allows at
    most one ghost row.

The ghost row itself is created on first use (lumen.services.erasure), not here.

The downgrade fails while a ghost row exists, on purpose: the restored
two-value CHECK rejects it, and that row owns erased users' history, which
must not be dropped silently.
"""

from alembic import op
import sqlalchemy as sa

revision = "b4c6d8e0f2a4"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

_COMMENTS = (
    ("entities", "entity_type",
     "'user' for OAuth users, 'project' for API projects, 'ghost' for the single shared erased-user entity",
     "'user' for OAuth users, 'project' for API projects"),
    ("api_keys", "key_hash",
     "HMAC-SHA256 of the raw key, keyed on ENCRYPTION_KEY",
     "SHA-256 hash of the raw key; null only during legacy migration"),
)


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    if _is_postgresql():
        op.execute("ALTER TABLE entities DROP CONSTRAINT ck_entities_type")
        op.execute(
            "ALTER TABLE entities ADD CONSTRAINT ck_entities_type"
            " CHECK (entity_type IN ('user', 'project', 'ghost'))"
        )
        for table, column, new, _old in _COMMENTS:
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_q(new)}")
    op.create_index(
        "uq_entities_single_ghost",
        "entities",
        ["entity_type"],
        unique=True,
        postgresql_where=sa.text("entity_type = 'ghost'"),
        sqlite_where=sa.text("entity_type = 'ghost'"),
    )


def downgrade():
    op.drop_index("uq_entities_single_ghost", table_name="entities")
    if _is_postgresql():
        op.execute("ALTER TABLE entities DROP CONSTRAINT ck_entities_type")
        # Fails while a ghost row exists; see the module docstring.
        op.execute(
            "ALTER TABLE entities ADD CONSTRAINT ck_entities_type"
            " CHECK (entity_type IN ('user', 'project'))"
        )
        for table, column, _new, old in _COMMENTS:
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_q(old)}")
