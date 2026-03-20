"""rename model_limits to entity_model_limits

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-19 00:01:00.000000

"""
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP INDEX IF EXISTS uq_ml_entity_model")
    op.execute("DROP INDEX IF EXISTS uq_ml_entity_default")
    op.rename_table("model_limits", "entity_model_limits")
    op.execute(
        "CREATE UNIQUE INDEX uq_eml_entity_model ON entity_model_limits (entity_id, model_config_id) "
        "WHERE model_config_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_eml_entity_default ON entity_model_limits (entity_id) "
        "WHERE model_config_id IS NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_eml_entity_default")
    op.execute("DROP INDEX IF EXISTS uq_eml_entity_model")
    op.rename_table("entity_model_limits", "model_limits")
    op.execute(
        "CREATE UNIQUE INDEX uq_ml_entity_model ON model_limits (entity_id, model_config_id) "
        "WHERE model_config_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ml_entity_default ON model_limits (entity_id) "
        "WHERE model_config_id IS NULL"
    )
