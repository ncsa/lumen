"""hash api keys in database

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-21 00:00:00.000000

"""
import hashlib
import hmac
import os
import sys

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    # Step 1: add new columns (nullable initially)
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('key_hint', sa.String(length=32), nullable=True))

    # Step 2: data migration — hash existing plaintext keys
    secret = os.environ.get("LUMEN_ENCRYPTION_KEY", "")
    if not secret:
        # Fall back to reading encryption_key from config.yaml
        import yaml
        config_yaml_path = os.environ.get("CONFIG_YAML", "./config.yaml")
        try:
            with open(config_yaml_path) as f:
                yaml_data = yaml.safe_load(f)
            secret = (yaml_data.get("app") or {}).get("encryption_key", "")
        except FileNotFoundError:
            pass
    if not secret:
        print(
            "\nERROR: encryption_key is not configured.\n"
            "Set it in config.yaml under app.encryption_key, or via the\n"
            "LUMEN_ENCRYPTION_KEY environment variable, then re-run:\n"
            "  flask db upgrade\n",
            file=sys.stderr,
        )
        raise SystemExit(1)

    conn = op.get_bind()
    api_keys = table(
        'api_keys',
        column('id', sa.Integer),
        column('key', sa.String),
        column('key_hash', sa.String),
        column('key_hint', sa.String),
    )
    rows = conn.execute(sa.select(api_keys.c.id, api_keys.c.key)).fetchall()
    for row in rows:
        key_hash = hmac.new(secret.encode(), row.key.encode(), hashlib.sha256).hexdigest()
        key_hint = f"{row.key[:7]}...{row.key[-4:]}"
        conn.execute(
            api_keys.update()
            .where(api_keys.c.id == row.id)
            .values(key_hash=key_hash, key_hint=key_hint)
        )

    # Step 3: make key_hash NOT NULL and add unique constraint; drop plaintext key column
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.alter_column('key_hash', nullable=False)
        batch_op.create_unique_constraint('uq_api_keys_key_hash', ['key_hash'])
        batch_op.drop_column('key')


def downgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key', sa.String(length=128), nullable=True))
        batch_op.drop_constraint('uq_api_keys_key_hash', type_='unique')
        batch_op.drop_column('key_hash')
        batch_op.drop_column('key_hint')
