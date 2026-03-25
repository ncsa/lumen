"""Migrate to single token pool per user with boolean model access

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-03-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    # -----------------------------------------------------------------------
    # Create new tables
    # -----------------------------------------------------------------------
    op.create_table(
        'entity_limits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('max_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('refresh_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('starting_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('config_managed', sa.Boolean(), nullable=False, server_default='false'),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entity_id'),
    )

    op.create_table(
        'entity_balances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('tokens_left', sa.BigInteger(), nullable=True),
        sa.Column('last_refill_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entity_id'),
    )

    op.create_table(
        'group_limits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('max_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('refresh_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('starting_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id'),
    )

    op.create_table(
        'entity_model_access',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('model_config_id', sa.Integer(), nullable=False),
        sa.Column('allowed', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entity_id', 'model_config_id', name='uq_ema_entity_model'),
    )

    op.create_table(
        'group_model_access',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('model_config_id', sa.Integer(), nullable=False),
        sa.Column('allowed', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'model_config_id', name='uq_gma_group_model'),
    )

    # -----------------------------------------------------------------------
    # Migrate data
    # -----------------------------------------------------------------------
    conn = op.get_bind()

    # entity_limits: for each entity, take best limit from entity_model_limits.
    # Priority: -2 (unlimited) wins; else highest positive max_tokens.
    # Rows with max_tokens = -1 (defer) are skipped unless they are the only option.
    rows = conn.execute(sa.text(
        "SELECT entity_id, max_tokens, refresh_tokens, starting_tokens, config_managed "
        "FROM entity_model_limits"
    )).fetchall()

    entity_best = {}
    for entity_id, max_tokens, refresh_tokens, starting_tokens, config_managed in rows:
        if entity_id not in entity_best:
            entity_best[entity_id] = (max_tokens, refresh_tokens, starting_tokens, config_managed)
        else:
            cur = entity_best[entity_id]
            # -2 (unlimited) wins all
            if max_tokens == -2:
                entity_best[entity_id] = (max_tokens, refresh_tokens, starting_tokens, config_managed)
            elif cur[0] == -2:
                pass  # keep current unlimited
            elif max_tokens > cur[0]:
                entity_best[entity_id] = (max_tokens, refresh_tokens, starting_tokens, config_managed)

    for entity_id, (max_t, refresh_t, starting_t, config_m) in entity_best.items():
        if max_t == -1:
            continue  # skip pure-defer rows, no pool
        conn.execute(sa.text(
            "INSERT INTO entity_limits (entity_id, max_tokens, refresh_tokens, starting_tokens, config_managed) "
            "VALUES (:eid, :max, :refresh, :starting, :cm)"
        ), {"eid": entity_id, "max": max_t, "refresh": refresh_t, "starting": starting_t, "cm": config_m})

    # entity_balances: for each entity, take max tokens_left across all model balances.
    balance_rows = conn.execute(sa.text(
        "SELECT entity_id, tokens_left, last_refill_at "
        "FROM entity_model_balances"
    )).fetchall()

    entity_balance_best = {}
    for entity_id, tokens_left, last_refill_at in balance_rows:
        if entity_id not in entity_balance_best:
            entity_balance_best[entity_id] = (tokens_left, last_refill_at)
        else:
            cur_tokens, cur_refill = entity_balance_best[entity_id]
            if tokens_left > cur_tokens:
                entity_balance_best[entity_id] = (tokens_left, last_refill_at)

    for entity_id, (tokens_left, last_refill_at) in entity_balance_best.items():
        conn.execute(sa.text(
            "INSERT INTO entity_balances (entity_id, tokens_left, last_refill_at) "
            "VALUES (:eid, :tl, :lra)"
        ), {"eid": entity_id, "tl": tokens_left, "lra": last_refill_at})

    # group_limits: use the NULL model_config_id (catch-all default) row per group.
    # Fall back to highest max across per-model rows if no default exists.
    group_limit_rows = conn.execute(sa.text(
        "SELECT group_id, model_config_id, max_tokens, refresh_tokens, starting_tokens "
        "FROM group_model_limits"
    )).fetchall()

    group_default = {}   # group_id -> (max, refresh, starting) from NULL row
    group_best = {}      # group_id -> (max, refresh, starting) best per-model row

    for group_id, model_config_id, max_tokens, refresh_tokens, starting_tokens in group_limit_rows:
        if model_config_id is None:
            group_default[group_id] = (max_tokens, refresh_tokens, starting_tokens)
        else:
            if group_id not in group_best:
                group_best[group_id] = (max_tokens, refresh_tokens, starting_tokens)
            else:
                cur = group_best[group_id]
                if max_tokens == -2 or (cur[0] != -2 and max_tokens > cur[0]):
                    group_best[group_id] = (max_tokens, refresh_tokens, starting_tokens)

    all_group_ids = set(group_default.keys()) | set(group_best.keys())
    for group_id in all_group_ids:
        pool = group_default.get(group_id) or group_best.get(group_id)
        if pool is None:
            continue
        max_t, refresh_t, starting_t = pool
        if max_t == -1:
            continue  # skip defer-only groups
        conn.execute(sa.text(
            "INSERT INTO group_limits (group_id, max_tokens, refresh_tokens, starting_tokens) "
            "VALUES (:gid, :max, :refresh, :starting)"
        ), {"gid": group_id, "max": max_t, "refresh": refresh_t, "starting": starting_t})

    # group_model_access: from per-model group_model_limits rows (model_config_id IS NOT NULL).
    # max_tokens > 0 or -2 -> allowed=True; 0 -> allowed=False; -1 (defer) -> skip.
    for group_id, model_config_id, max_tokens, refresh_tokens, starting_tokens in group_limit_rows:
        if model_config_id is None:
            continue
        if max_tokens == -1:
            continue  # defer = no explicit access row
        allowed = max_tokens != 0
        conn.execute(sa.text(
            "INSERT INTO group_model_access (group_id, model_config_id, allowed) "
            "VALUES (:gid, :mid, :allowed)"
        ), {"gid": group_id, "mid": model_config_id, "allowed": allowed})

    # -----------------------------------------------------------------------
    # Drop old tables
    # -----------------------------------------------------------------------
    op.drop_table('entity_model_balances')
    op.drop_table('entity_model_limits')
    op.drop_table('group_model_limits')


def downgrade():
    # Recreate old tables
    op.create_table(
        'group_model_limits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('model_config_id', sa.Integer(), nullable=True),
        sa.Column('max_tokens', sa.BigInteger(), nullable=False, server_default='-1'),
        sa.Column('refresh_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('starting_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'entity_model_limits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('model_config_id', sa.Integer(), nullable=True),
        sa.Column('max_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('refresh_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('starting_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('config_managed', sa.Boolean(), nullable=False, server_default='false'),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'entity_model_balances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('model_config_id', sa.Integer(), nullable=False),
        sa.Column('tokens_left', sa.BigInteger(), nullable=True),
        sa.Column('last_refill_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entity_id', 'model_config_id', name='uq_emb_entity_model'),
    )

    conn = op.get_bind()

    # Restore entity_model_limits as a single default (NULL model_config_id) row per entity
    entity_limit_rows = conn.execute(sa.text(
        "SELECT entity_id, max_tokens, refresh_tokens, starting_tokens, config_managed FROM entity_limits"
    )).fetchall()
    for entity_id, max_t, refresh_t, starting_t, config_m in entity_limit_rows:
        conn.execute(sa.text(
            "INSERT INTO entity_model_limits (entity_id, model_config_id, max_tokens, refresh_tokens, starting_tokens, config_managed) "
            "VALUES (:eid, NULL, :max, :refresh, :starting, :cm)"
        ), {"eid": entity_id, "max": max_t, "refresh": refresh_t, "starting": starting_t, "cm": config_m})

    # Restore entity_model_balances: can't recover per-model rows, skip
    # (data loss on downgrade is acceptable)

    # Restore group_model_limits as NULL row per group
    group_limit_rows = conn.execute(sa.text(
        "SELECT group_id, max_tokens, refresh_tokens, starting_tokens FROM group_limits"
    )).fetchall()
    for group_id, max_t, refresh_t, starting_t in group_limit_rows:
        conn.execute(sa.text(
            "INSERT INTO group_model_limits (group_id, model_config_id, max_tokens, refresh_tokens, starting_tokens) "
            "VALUES (:gid, NULL, :max, :refresh, :starting)"
        ), {"gid": group_id, "max": max_t, "refresh": refresh_t, "starting": starting_t})

    # Restore per-model group_model_limits from group_model_access
    access_rows = conn.execute(sa.text(
        "SELECT group_id, model_config_id, allowed FROM group_model_access"
    )).fetchall()
    for group_id, model_config_id, allowed in access_rows:
        max_t = -2 if allowed else 0  # best effort: can't restore original values
        conn.execute(sa.text(
            "INSERT INTO group_model_limits (group_id, model_config_id, max_tokens, refresh_tokens, starting_tokens) "
            "VALUES (:gid, :mid, :max, 0, 0)"
        ), {"gid": group_id, "mid": model_config_id, "max": max_t})

    op.drop_table('group_model_access')
    op.drop_table('entity_model_access')
    op.drop_table('group_limits')
    op.drop_table('entity_balances')
    op.drop_table('entity_limits')
