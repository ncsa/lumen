"""Add COMMENT ON TABLE/COLUMN to all tables (PostgreSQL only)

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-05-08 00:00:00.000000

"""

from alembic import op

revision = "v2w3x4y5z6a7"
down_revision = "u1v2w3x4y5z6"
branch_labels = None
depends_on = None

# (table, column_or_None, comment)  — column=None means COMMENT ON TABLE
_COMMENTS = [
    # entities
    ("entities", None, "Human users (OAuth) and programmatic clients (API key); entity_type distinguishes them"),
    ("entities", "id", "Primary key"),
    ("entities", "entity_type", "'user' for OAuth users, 'client' for API clients"),
    ("entities", "email", "User email address; null for clients"),
    ("entities", "name", "Display name"),
    ("entities", "initials", "Short initials for UI avatars"),
    ("entities", "gravatar_hash", "MD5 hash of email for Gravatar lookups; users only"),
    ("entities", "active", "Inactive entities are blocked from making requests"),
    ("entities", "created_at", "UTC creation timestamp"),
    ("entities", "model_access_default", "Default model access policy: whitelist, blacklist, or graylist; client entities only"),

    # api_keys
    ("api_keys", None, "API keys for entity authentication; only the SHA-256 hash is stored, never the plaintext"),
    ("api_keys", "id", "Primary key"),
    ("api_keys", "entity_id", "Owning entity"),
    ("api_keys", "name", "Human-readable label for the key"),
    ("api_keys", "key_hash", "SHA-256 hash of the raw key; null only during legacy migration"),
    ("api_keys", "key_hint", "Last few characters of the raw key shown in the UI for identification"),
    ("api_keys", "active", "Inactive keys are rejected on all requests"),
    ("api_keys", "requests", "Cumulative request count made with this key"),
    ("api_keys", "input_tokens", "Cumulative input tokens consumed via this key"),
    ("api_keys", "output_tokens", "Cumulative output tokens produced via this key"),
    ("api_keys", "cost", "Cumulative cost in USD charged through this key"),
    ("api_keys", "last_used_at", "UTC timestamp of the most recent request; null if never used"),
    ("api_keys", "created_at", "UTC creation timestamp"),

    # model_configs
    ("model_configs", None, "Configuration and metadata for each AI model that Lumen can proxy"),
    ("model_configs", "id", "Primary key"),
    ("model_configs", "model_name", "Canonical model identifier exposed to clients, e.g. gpt-4o"),
    ("model_configs", "input_cost_per_million", "USD cost per one million input tokens"),
    ("model_configs", "output_cost_per_million", "USD cost per one million output tokens"),
    ("model_configs", "active", "Inactive models are hidden and cannot be used"),
    ("model_configs", "description", "Human-readable description shown in the UI"),
    ("model_configs", "url", "Link to provider documentation"),
    ("model_configs", "max_input_tokens", "Deprecated; use context_window instead"),
    ("model_configs", "supports_function_calling", "Whether the model supports tool/function-calling"),
    ("model_configs", "input_modalities", 'Supported input types, e.g. ["text", "image"]'),
    ("model_configs", "output_modalities", 'Supported output types, e.g. ["text"]'),
    ("model_configs", "context_window", "Total context window in tokens (input + output)"),
    ("model_configs", "max_output_tokens", "Maximum tokens the model can generate per response"),
    ("model_configs", "supports_reasoning", "Whether the model exposes chain-of-thought reasoning tokens"),
    ("model_configs", "knowledge_cutoff", "Training data cutoff in YYYY-MM format"),
    ("model_configs", "notice", "Optional admin notice displayed to users on the model detail page"),
    ("model_configs", "created_at", "UTC creation timestamp"),

    # model_endpoints
    ("model_endpoints", None, "Backend endpoints for model_configs; multiple endpoints enable load distribution and failover"),
    ("model_endpoints", "id", "Primary key"),
    ("model_endpoints", "model_config_id", "Parent model configuration"),
    ("model_endpoints", "url", "Base URL of the upstream API"),
    ("model_endpoints", "api_key", "Credential forwarded to the upstream API"),
    ("model_endpoints", "model_name", "Override model name sent upstream; null means use model_config.model_name"),
    ("model_endpoints", "healthy", "Last known health status, updated by the health-check background task"),
    ("model_endpoints", "last_checked_at", "UTC timestamp of the most recent health check; null if never checked"),
    ("model_endpoints", "created_at", "UTC creation timestamp"),

    # entity_limits
    ("entity_limits", None, "Coin budget configuration per entity; -2=unlimited, 0=blocked, positive=budget"),
    ("entity_limits", "id", "Primary key"),
    ("entity_limits", "entity_id", "The entity this limit applies to; one row per entity"),
    ("entity_limits", "max_coins", "Maximum coins the entity may hold; -2=unlimited, 0=blocked"),
    ("entity_limits", "refresh_coins", "Coins added on each periodic refill cycle"),
    ("entity_limits", "starting_coins", "Coins granted at entity creation or after a balance reset"),
    ("entity_limits", "config_managed", "When true, owned by config.yaml and must not be edited via the UI"),

    # entity_balances
    ("entity_balances", None, "Current coin balance per entity; decremented on requests, replenished by the refill job"),
    ("entity_balances", "id", "Primary key"),
    ("entity_balances", "entity_id", "The entity this balance belongs to; one row per entity"),
    ("entity_balances", "coins_left", "Current spendable coin balance"),
    ("entity_balances", "last_refill_at", "UTC timestamp of the most recent coin refill"),

    # entity_model_access
    ("entity_model_access", None, "Per-entity model access overrides; entity-level takes precedence over group-level"),
    ("entity_model_access", "id", "Primary key"),
    ("entity_model_access", "entity_id", "The entity the override applies to"),
    ("entity_model_access", "model_config_id", "The model being overridden"),
    ("entity_model_access", "access_type", "whitelist (always allowed), blacklist (always denied), or graylist (requires consent)"),

    # entity_model_consents
    ("entity_model_consents", None, "Records entity acceptance of a graylisted model notice; a row is required before use"),
    ("entity_model_consents", "id", "Primary key"),
    ("entity_model_consents", "entity_id", "The consenting entity"),
    ("entity_model_consents", "model_config_id", "The model for which consent was given"),
    ("entity_model_consents", "consented_at", "UTC timestamp when the entity accepted the model notice"),

    # groups
    ("groups", None, "Named collections of entities for bulk model access and coin limit policy assignment"),
    ("groups", "id", "Primary key"),
    ("groups", "name", "Unique group identifier"),
    ("groups", "description", "Optional description shown in the admin UI"),
    ("groups", "active", "Inactive groups have no effect on member access"),
    ("groups", "config_managed", "When true, group and membership are controlled by config.yaml"),
    ("groups", "model_access_default", "Default model access policy for this group: whitelist, blacklist, or graylist"),
    ("groups", "created_at", "UTC creation timestamp"),

    # group_members
    ("group_members", None, "Association between entities and groups; an entity may belong to multiple groups"),
    ("group_members", "id", "Primary key"),
    ("group_members", "group_id", "The group"),
    ("group_members", "entity_id", "The member entity"),
    ("group_members", "config_managed", "When true, created by config.yaml and must not be removed via the UI"),

    # group_limits
    ("group_limits", None, "Coin budget configuration per group; -2=unlimited, 0=blocked, positive=budget"),
    ("group_limits", "id", "Primary key"),
    ("group_limits", "group_id", "The group this limit applies to; one row per group"),
    ("group_limits", "max_coins", "Maximum coins the group may hold; -2=unlimited, 0=blocked"),
    ("group_limits", "refresh_coins", "Coins added on each periodic refill cycle"),
    ("group_limits", "starting_coins", "Coins granted at group creation or after a balance reset"),

    # group_model_access
    ("group_model_access", None, "Per-group model access overrides; lower priority than entity_model_access rows"),
    ("group_model_access", "id", "Primary key"),
    ("group_model_access", "group_id", "The group the override applies to"),
    ("group_model_access", "model_config_id", "The model being overridden"),
    ("group_model_access", "access_type", "whitelist (always allowed), blacklist (always denied), or graylist (requires consent)"),

    # entity_managers
    ("entity_managers", None, "Maps users to client entities they are permitted to manage"),
    ("entity_managers", "id", "Primary key"),
    ("entity_managers", "user_entity_id", "The user who has management rights over the client"),
    ("entity_managers", "client_entity_id", "The client entity being managed"),

    # model_stats
    ("model_stats", None, "Aggregated usage counters per (entity, model, source) triple; updated on every proxied request"),
    ("model_stats", "id", "Primary key"),
    ("model_stats", "entity_id", "The entity that made the requests"),
    ("model_stats", "model_config_id", "The model used"),
    ("model_stats", "source", "Origin of the requests: chat (web UI) or api (API key)"),
    ("model_stats", "requests", "Total request count"),
    ("model_stats", "input_tokens", "Total input tokens consumed"),
    ("model_stats", "output_tokens", "Total output tokens produced"),
    ("model_stats", "cost", "Total cost in USD"),
    ("model_stats", "last_used_at", "UTC timestamp of the most recent counted request"),

    # conversations
    ("conversations", None, "Chat sessions created through the Lumen web UI; hidden=true is a soft delete"),
    ("conversations", "id", "Primary key"),
    ("conversations", "entity_id", "The owning user entity"),
    ("conversations", "title", "Short auto-generated or user-edited title"),
    ("conversations", "model", "Snapshot of the model name at conversation creation time"),
    ("conversations", "hidden", "Soft-delete flag; hidden conversations are not shown in the UI"),
    ("conversations", "created_at", "UTC creation timestamp"),
    ("conversations", "updated_at", "UTC timestamp of the most recent message or edit"),

    # messages
    ("messages", None, "Individual turns within a conversation; performance metadata columns are assistant-only"),
    ("messages", "id", "Primary key"),
    ("messages", "conversation_id", "The parent conversation"),
    ("messages", "role", "Speaker role: user, assistant, or system"),
    ("messages", "content", "Full message text"),
    ("messages", "created_at", "UTC creation timestamp"),
    ("messages", "input_tokens", "Input tokens reported by the model; assistant messages only"),
    ("messages", "output_tokens", "Output tokens reported by the model; assistant messages only"),
    ("messages", "time_to_first_token", "Seconds from request send to first token received; assistant messages only"),
    ("messages", "duration", "Total response time in seconds; assistant messages only"),
    ("messages", "output_speed", "Output tokens per second; assistant messages only"),

    # request_logs
    ("request_logs", None, "Append-only request log; TimescaleDB hypertable on PostgreSQL, plain table on SQLite"),
    ("request_logs", "time", "UTC request timestamp; TimescaleDB partition key"),
    ("request_logs", "entity_id", "Requesting entity; SET NULL on delete to preserve historical data"),
    ("request_logs", "model_config_id", "Model used; SET NULL on delete to preserve historical data"),
    ("request_logs", "model_endpoint_id", "Backend endpoint that served the request; SET NULL on delete to preserve historical data"),
    ("request_logs", "source", "Origin of the request: chat (web UI) or api (API key)"),
    ("request_logs", "input_tokens", "Input token count for this request"),
    ("request_logs", "output_tokens", "Output token count for this request"),
    ("request_logs", "cost", "Cost in USD for this request"),
    ("request_logs", "duration", "Total proxy response time in seconds"),
]


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    # Use indexed dollar-quoting to avoid collisions with any $$ in the text
    return f"$comment${text}$comment$"


def upgrade():
    if not _is_postgresql():
        return
    for table, column, comment in _COMMENTS:
        if column is None:
            op.execute(f"COMMENT ON TABLE {table} IS {_q(comment)}")
        else:
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_q(comment)}")


def downgrade():
    if not _is_postgresql():
        return
    for table, column, _ in _COMMENTS:
        if column is None:
            op.execute(f"COMMENT ON TABLE {table} IS NULL")
        else:
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS NULL")
