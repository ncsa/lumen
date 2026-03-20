"""add config_managed to groups, group_members, entity_model_limits

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('groups', schema=None) as batch_op:
        batch_op.add_column(sa.Column('config_managed', sa.Boolean(), nullable=False, server_default='0'))

    with op.batch_alter_table('group_members', schema=None) as batch_op:
        batch_op.add_column(sa.Column('config_managed', sa.Boolean(), nullable=False, server_default='0'))

    with op.batch_alter_table('entity_model_limits', schema=None) as batch_op:
        batch_op.add_column(sa.Column('config_managed', sa.Boolean(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('entity_model_limits', schema=None) as batch_op:
        batch_op.drop_column('config_managed')

    with op.batch_alter_table('group_members', schema=None) as batch_op:
        batch_op.drop_column('config_managed')

    with op.batch_alter_table('groups', schema=None) as batch_op:
        batch_op.drop_column('config_managed')
