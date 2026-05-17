"""Add missing indexes for FK columns and query-heavy columns

Revision ID: z0a1b2c3d4e5
Revises: y9z0a1b2c3d4
Create Date: 2026-05-16 00:00:00.000000

"""

from alembic import op

revision = "z0a1b2c3d4e5"
down_revision = "y9z0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    # Composite index for list_conversations query (entity_id + hidden + updated_at)
    op.create_index(
        "ix_conversations_entity_hidden_updated",
        "conversations",
        ["entity_id", "hidden", "updated_at"],
    )

    # Messages: FK-side index for conversation history load
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    # Group members: FK-side index for entity membership lookups (_get_active_group_ids)
    op.create_index("ix_group_members_entity_id", "group_members", ["entity_id"])

    # Entity model access: FK-side index for access checks
    op.create_index("ix_entity_model_access_entity_id", "entity_model_access", ["entity_id"])

    # Group model access: FK-side index for group-level access checks
    op.create_index("ix_group_model_access_group_id", "group_model_access", ["group_id"])

    # Model stats: FK-side indexes for analytics and usage queries
    op.create_index("ix_model_stats_entity_id", "model_stats", ["entity_id"])
    op.create_index("ix_model_stats_model_config_id", "model_stats", ["model_config_id"])

    # Request logs: FK-side indexes for model detail page queries
    op.create_index("ix_request_logs_entity_id", "request_logs", ["entity_id"])
    op.create_index("ix_request_logs_model_config_id", "request_logs", ["model_config_id"])

    # API keys: FK-side index for profile page key listing
    op.create_index("ix_api_keys_entity_id", "api_keys", ["entity_id"])


def downgrade():
    op.drop_index("ix_api_keys_entity_id", table_name="api_keys")
    op.drop_index("ix_request_logs_model_config_id", table_name="request_logs")
    op.drop_index("ix_request_logs_entity_id", table_name="request_logs")
    op.drop_index("ix_model_stats_model_config_id", table_name="model_stats")
    op.drop_index("ix_model_stats_entity_id", table_name="model_stats")
    op.drop_index("ix_group_model_access_group_id", table_name="group_model_access")
    op.drop_index("ix_entity_model_access_entity_id", table_name="entity_model_access")
    op.drop_index("ix_group_members_entity_id", table_name="group_members")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_index("ix_conversations_entity_hidden_updated", table_name="conversations")
