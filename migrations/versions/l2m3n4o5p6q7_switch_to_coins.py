"""Switch token budget to coins

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-04-28 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    if _is_postgresql():
        # entity_limits: rename, reset large values (while still BIGINT), then retype
        op.execute("ALTER TABLE entity_limits RENAME COLUMN max_tokens TO max_coins")
        op.execute("ALTER TABLE entity_limits RENAME COLUMN refresh_tokens TO refresh_coins")
        op.execute("ALTER TABLE entity_limits RENAME COLUMN starting_tokens TO starting_coins")
        # Reset old token counts (millions) to 0 before narrowing type; keep -2 (unlimited)
        op.execute("UPDATE entity_limits SET max_coins = 0 WHERE max_coins > 0")
        op.execute("UPDATE entity_limits SET refresh_coins = 0")
        op.execute("UPDATE entity_limits SET starting_coins = 0")
        op.execute("ALTER TABLE entity_limits ALTER COLUMN max_coins TYPE NUMERIC(12,6)")
        op.execute("ALTER TABLE entity_limits ALTER COLUMN refresh_coins TYPE NUMERIC(12,6)")
        op.execute("ALTER TABLE entity_limits ALTER COLUMN starting_coins TYPE NUMERIC(12,6)")

        # group_limits: same order — rename, reset, retype
        op.execute("ALTER TABLE group_limits RENAME COLUMN max_tokens TO max_coins")
        op.execute("ALTER TABLE group_limits RENAME COLUMN refresh_tokens TO refresh_coins")
        op.execute("ALTER TABLE group_limits RENAME COLUMN starting_tokens TO starting_coins")
        op.execute("UPDATE group_limits SET max_coins = 0 WHERE max_coins > 0")
        op.execute("UPDATE group_limits SET refresh_coins = 0")
        op.execute("UPDATE group_limits SET starting_coins = 0")
        op.execute("ALTER TABLE group_limits ALTER COLUMN max_coins TYPE NUMERIC(12,6)")
        op.execute("ALTER TABLE group_limits ALTER COLUMN refresh_coins TYPE NUMERIC(12,6)")
        op.execute("ALTER TABLE group_limits ALTER COLUMN starting_coins TYPE NUMERIC(12,6)")

        # entity_balances: rename, reset to 20 coins (while still BIGINT), then retype
        op.execute("ALTER TABLE entity_balances RENAME COLUMN tokens_left TO coins_left")
        op.execute("UPDATE entity_balances SET coins_left = 20")
        op.execute("ALTER TABLE entity_balances ALTER COLUMN coins_left TYPE NUMERIC(12,6)")
    else:
        # SQLite: use batch migration (recreates tables)
        with op.batch_alter_table('entity_limits') as batch_op:
            batch_op.alter_column('max_tokens',
                                  new_column_name='max_coins',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.BigInteger())
            batch_op.alter_column('refresh_tokens',
                                  new_column_name='refresh_coins',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.Integer())
            batch_op.alter_column('starting_tokens',
                                  new_column_name='starting_coins',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.BigInteger())
        op.execute("UPDATE entity_limits SET max_coins = 0 WHERE max_coins > 0")
        op.execute("UPDATE entity_limits SET refresh_coins = 0, starting_coins = 0")

        with op.batch_alter_table('group_limits') as batch_op:
            batch_op.alter_column('max_tokens',
                                  new_column_name='max_coins',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.BigInteger())
            batch_op.alter_column('refresh_tokens',
                                  new_column_name='refresh_coins',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.Integer())
            batch_op.alter_column('starting_tokens',
                                  new_column_name='starting_coins',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.BigInteger())
        op.execute("UPDATE group_limits SET max_coins = 0 WHERE max_coins > 0")
        op.execute("UPDATE group_limits SET refresh_coins = 0, starting_coins = 0")

        with op.batch_alter_table('entity_balances') as batch_op:
            batch_op.alter_column('tokens_left',
                                  new_column_name='coins_left',
                                  type_=sa.Numeric(12, 6),
                                  existing_type=sa.BigInteger())
        op.execute("UPDATE entity_balances SET coins_left = 20")


def downgrade():
    if _is_postgresql():
        op.execute("ALTER TABLE entity_balances RENAME COLUMN coins_left TO tokens_left")
        op.execute("ALTER TABLE entity_balances ALTER COLUMN tokens_left TYPE BIGINT USING tokens_left::bigint")

        op.execute("ALTER TABLE entity_limits RENAME COLUMN max_coins TO max_tokens")
        op.execute("ALTER TABLE entity_limits ALTER COLUMN max_tokens TYPE BIGINT USING max_tokens::bigint")
        op.execute("ALTER TABLE entity_limits RENAME COLUMN refresh_coins TO refresh_tokens")
        op.execute("ALTER TABLE entity_limits ALTER COLUMN refresh_tokens TYPE INTEGER USING refresh_tokens::integer")
        op.execute("ALTER TABLE entity_limits RENAME COLUMN starting_coins TO starting_tokens")
        op.execute("ALTER TABLE entity_limits ALTER COLUMN starting_tokens TYPE BIGINT USING starting_tokens::bigint")

        op.execute("ALTER TABLE group_limits RENAME COLUMN max_coins TO max_tokens")
        op.execute("ALTER TABLE group_limits ALTER COLUMN max_tokens TYPE BIGINT USING max_tokens::bigint")
        op.execute("ALTER TABLE group_limits RENAME COLUMN refresh_coins TO refresh_tokens")
        op.execute("ALTER TABLE group_limits ALTER COLUMN refresh_tokens TYPE INTEGER USING refresh_tokens::integer")
        op.execute("ALTER TABLE group_limits RENAME COLUMN starting_coins TO starting_tokens")
        op.execute("ALTER TABLE group_limits ALTER COLUMN starting_tokens TYPE BIGINT USING starting_tokens::bigint")
    else:
        with op.batch_alter_table('entity_balances') as batch_op:
            batch_op.alter_column('coins_left',
                                  new_column_name='tokens_left',
                                  type_=sa.BigInteger(),
                                  existing_type=sa.Numeric(12, 6))

        with op.batch_alter_table('entity_limits') as batch_op:
            batch_op.alter_column('max_coins',
                                  new_column_name='max_tokens',
                                  type_=sa.BigInteger(),
                                  existing_type=sa.Numeric(12, 6))
            batch_op.alter_column('refresh_coins',
                                  new_column_name='refresh_tokens',
                                  type_=sa.Integer(),
                                  existing_type=sa.Numeric(12, 6))
            batch_op.alter_column('starting_coins',
                                  new_column_name='starting_tokens',
                                  type_=sa.BigInteger(),
                                  existing_type=sa.Numeric(12, 6))

        with op.batch_alter_table('group_limits') as batch_op:
            batch_op.alter_column('max_coins',
                                  new_column_name='max_tokens',
                                  type_=sa.BigInteger(),
                                  existing_type=sa.Numeric(12, 6))
            batch_op.alter_column('refresh_coins',
                                  new_column_name='refresh_tokens',
                                  type_=sa.Integer(),
                                  existing_type=sa.Numeric(12, 6))
            batch_op.alter_column('starting_coins',
                                  new_column_name='starting_tokens',
                                  type_=sa.BigInteger(),
                                  existing_type=sa.Numeric(12, 6))
