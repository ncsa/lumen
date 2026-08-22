"""Encrypt model_endpoints.api_key at rest and widen the column for ciphertext

Revision ID: z5c6d7e8f9a0
Revises: ag7b8c9d0e1f
Create Date: 2026-08-20 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "z5c6d7e8f9a0"
down_revision = "ag7b8c9d0e1f"
branch_labels = None
depends_on = None


def upgrade():
    # Fernet ciphertext of a 256-char key is ~500 chars, so String(256) must widen.
    with op.batch_alter_table("model_endpoints") as batch_op:
        batch_op.alter_column(
            "api_key",
            existing_type=sa.String(256),
            type_=sa.Text(),
            existing_nullable=False,
            comment="Credential forwarded to the upstream API, encrypted at rest with the app encryption key",
            existing_comment="Credential forwarded to the upstream API",
        )

    # Encrypt existing plaintext rows. Runs under `flask db upgrade`, so the app
    # context (and its ENCRYPTION_KEY) is available. Already-encrypted values
    # (the "enc:" prefix) are left alone, making the migration re-runnable.
    from lumen.services.crypto import _ENC_PREFIX, encrypt_secret

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, api_key FROM model_endpoints")).all()
    for row_id, api_key in rows:
        if api_key and not api_key.startswith(_ENC_PREFIX):
            conn.execute(
                sa.text("UPDATE model_endpoints SET api_key = :key WHERE id = :id"),
                {"key": encrypt_secret(api_key), "id": row_id},
            )


def downgrade():
    from lumen.services.crypto import decrypt_secret

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, api_key FROM model_endpoints")).all()
    for row_id, api_key in rows:
        if api_key:
            conn.execute(
                sa.text("UPDATE model_endpoints SET api_key = :key WHERE id = :id"),
                {"key": decrypt_secret(api_key), "id": row_id},
            )

    with op.batch_alter_table("model_endpoints") as batch_op:
        batch_op.alter_column(
            "api_key",
            existing_type=sa.Text(),
            type_=sa.String(256),
            existing_nullable=False,
            comment="Credential forwarded to the upstream API",
            existing_comment="Credential forwarded to the upstream API, encrypted at rest with the app encryption key",
        )
