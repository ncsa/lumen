"""fix missing autoincrement sequences on integer primary keys

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-22 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'g7h8i9j0k1l2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


# All tables with a single-column integer primary key named 'id'.
_TABLES = [
    'entities',
    'model_configs',
    'api_keys',
    'entity_managers',
    'model_endpoints',
    'entity_model_limits',
    'entity_model_balances',
    'model_stats',
    'conversations',
    'messages',
    'groups',
    'group_members',
    'group_model_limits',
]


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return

    # For each table, create a sequence and attach it as the column default if
    # one is not already present.  This is idempotent: tables that already have
    # a SERIAL/sequence default are left untouched.
    for table in _TABLES:
        op.execute(f"""
            DO $$
            DECLARE
                seq_name TEXT := '{table}_id_seq';
                max_id   BIGINT;
            BEGIN
                -- Skip if column already has a default (sequence already present)
                IF (
                    SELECT column_default
                    FROM information_schema.columns
                    WHERE table_name = '{table}' AND column_name = 'id'
                ) IS NOT NULL THEN
                    RETURN;
                END IF;

                CREATE SEQUENCE IF NOT EXISTS {table}_id_seq;
                EXECUTE format('SELECT COALESCE(MAX(id), 0) FROM {table}') INTO max_id;
                PERFORM setval('{table}_id_seq', max_id + 1);
                ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{table}_id_seq');
                ALTER SEQUENCE {table}_id_seq OWNED BY {table}.id;
            END
            $$;
        """)


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return

    # Remove the sequences added by upgrade (only if they were created by us,
    # i.e. the default is still pointing at our sequence name).
    for table in _TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF (
                    SELECT column_default
                    FROM information_schema.columns
                    WHERE table_name = '{table}' AND column_name = 'id'
                ) = 'nextval(''{table}_id_seq''::regclass)' THEN
                    ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT;
                    DROP SEQUENCE IF EXISTS {table}_id_seq;
                END IF;
            END
            $$;
        """)
