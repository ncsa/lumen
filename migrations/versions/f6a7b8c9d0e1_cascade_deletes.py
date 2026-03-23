"""add ON DELETE CASCADE to all foreign keys

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-22 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


# (table, fk_cols, ref_table, ref_cols, new_constraint_name)
_CASCADE_FKS = [
    ('api_keys',              ['entity_id'],         'entities',      ['id'], 'fk_api_keys_entity_id'),
    ('conversations',         ['entity_id'],         'entities',      ['id'], 'fk_conversations_entity_id'),
    ('entity_managers',       ['user_entity_id'],    'entities',      ['id'], 'fk_entity_managers_user_entity_id'),
    ('entity_managers',       ['service_entity_id'], 'entities',      ['id'], 'fk_entity_managers_service_entity_id'),
    ('entity_model_balances', ['entity_id'],         'entities',      ['id'], 'fk_entity_model_balances_entity_id'),
    ('entity_model_balances', ['model_config_id'],   'model_configs', ['id'], 'fk_entity_model_balances_model_config_id'),
    ('entity_model_limits',   ['entity_id'],         'entities',      ['id'], 'fk_entity_model_limits_entity_id'),
    ('entity_model_limits',   ['model_config_id'],   'model_configs', ['id'], 'fk_entity_model_limits_model_config_id'),
    ('group_members',         ['group_id'],          'groups',        ['id'], 'fk_group_members_group_id'),
    ('group_members',         ['entity_id'],         'entities',      ['id'], 'fk_group_members_entity_id'),
    ('group_model_limits',    ['group_id'],          'groups',        ['id'], 'fk_group_model_limits_group_id'),
    ('group_model_limits',    ['model_config_id'],   'model_configs', ['id'], 'fk_group_model_limits_model_config_id'),
    ('messages',              ['conversation_id'],   'conversations', ['id'], 'fk_messages_conversation_id'),
    ('model_endpoints',       ['model_config_id'],   'model_configs', ['id'], 'fk_model_endpoints_model_config_id'),
    ('model_stats',           ['entity_id'],         'entities',      ['id'], 'fk_model_stats_entity_id'),
    ('model_stats',           ['model_config_id'],   'model_configs', ['id'], 'fk_model_stats_model_config_id'),
]


def _get_fk_name(inspector, table, constrained_cols):
    """Return the DB-level name of the FK constraint for the given columns, or None."""
    for fk in inspector.get_foreign_keys(table):
        if fk['constrained_columns'] == constrained_cols:
            return fk.get('name')
    return None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, fk_cols, ref_table, ref_cols, new_name in _CASCADE_FKS:
        old_name = _get_fk_name(inspector, table, fk_cols)
        with op.batch_alter_table(table, schema=None) as batch_op:
            if old_name:
                batch_op.drop_constraint(old_name, type_='foreignkey')
            batch_op.create_foreign_key(
                new_name, ref_table, fk_cols, ref_cols, ondelete='CASCADE'
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, fk_cols, ref_table, ref_cols, new_name in reversed(_CASCADE_FKS):
        old_name = _get_fk_name(inspector, table, fk_cols)
        with op.batch_alter_table(table, schema=None) as batch_op:
            if old_name:
                batch_op.drop_constraint(old_name, type_='foreignkey')
            batch_op.create_foreign_key(
                f'{new_name}_noaction', ref_table, fk_cols, ref_cols
            )
