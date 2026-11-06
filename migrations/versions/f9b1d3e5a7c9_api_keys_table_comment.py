"""Correct the api_keys table comment: keys are stored as an HMAC-SHA256

Revision ID: f9b1d3e5a7c9
Revises: e8a0c2d4f6b8
Create Date: 2026-11-06 00:00:00.000000

``v2w3x4y5z6a7`` wrote "only the SHA-256 hash is stored" as the table comment,
but the key column holds an HMAC-SHA256 of the key, keyed on ``ENCRYPTION_KEY``
(see ``hash_api_key``). The model now says so; this brings existing databases
in line. Comment only, PostgreSQL only; a no-op on SQLite, which has no table
comments.
"""

from alembic import op

revision = "f9b1d3e5a7c9"
down_revision = "e8a0c2d4f6b8"
branch_labels = None
depends_on = None

_OLD = "API keys for entity authentication; only the SHA-256 hash is stored, never the plaintext"
_NEW = "API keys for entity authentication; only an HMAC-SHA256 of the key is stored, never the plaintext"


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.create_table_comment("api_keys", _NEW, existing_comment=_OLD)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.create_table_comment("api_keys", _OLD, existing_comment=_NEW)
