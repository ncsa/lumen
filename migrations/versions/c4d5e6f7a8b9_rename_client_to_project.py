"""Rename entity_type value 'client' to 'project' and client_entity_id column

Revision ID: c4d5e6f7a8b9
Revises: b7c8d9e0f1a2
Create Date: 2026-07-04 00:00:00.000000

Renames the lumen "client" domain concept to "project":
  * entities.entity_type value 'client' -> 'project' (plus its CHECK constraint)
  * entity_managers.client_entity_id column -> project_entity_id (plus its index)
  * schema comments referencing "client" -> "project"

The CHECK constraint is recreated with raw ALTER TABLE (PostgreSQL only),
matching b2c3d4e5f6g7. SQLite dev uses create_all + stamp head and never runs
this chain, so the CHECK step does not need to be SQLite-portable.
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


# (table, column_or_None, comment) — column=None means COMMENT ON TABLE.
# Only the entries whose text changed from "client" to "project" are listed;
# other schema comments are left untouched (set by v2w3x4y5z6a7).
_COMMENTS = [
    ("entities", None, "Human users (OAuth) and programmatic projects (API key); entity_type distinguishes them"),
    ("entities", "entity_type", "'user' for OAuth users, 'project' for API projects"),
    ("entities", "email", "User email address; null for projects"),
    ("entities", "model_access_default", "Default model access policy: 'allowed' or 'blocked'; project entities only"),
    ("entity_managers", None, "Maps users to project entities they are permitted to manage"),
    ("entity_managers", "user_entity_id", "The user who has management rights over the project"),
    ("entity_managers", "project_entity_id", "The project entity being managed"),
]


def upgrade():
    # 1. Rename the entity_managers.client_entity_id column. batch_alter_table
    #    matches the s9t0u1v2w3x4 precedent (service_entity_id -> client_entity_id).
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.alter_column("client_entity_id", new_column_name="project_entity_id")

    # 2. Recreate the index under the new name. Renaming a column does not
    #    rename its index on PostgreSQL, and SQLite dev skips this chain, so a
    #    drop-if-exists + create is the safe cross-dialect shape.
    op.execute("DROP INDEX IF EXISTS ix_entity_managers_client_entity_id")
    op.create_index("ix_entity_managers_project_entity_id", "entity_managers", ["project_entity_id"])

    # 3. Migrate the discriminator value and its CHECK constraint (PostgreSQL
    #    raw SQL, matching b2c3d4e5f6g7). Drop the old CHECK before the UPDATE
    #    so the new value is not rejected; re-add with the new value set.
    op.execute("ALTER TABLE entities DROP CONSTRAINT ck_entities_type")
    op.execute("UPDATE entities SET entity_type = 'project' WHERE entity_type = 'client'")
    op.execute("ALTER TABLE entities ADD CONSTRAINT ck_entities_type CHECK (entity_type IN ('user', 'project'))")

    # 4. Refresh schema comments (PostgreSQL only), matching v2w3x4y5z6a7.
    if _is_postgresql():
        for table, column, comment in _COMMENTS:
            if column is None:
                op.execute(f"COMMENT ON TABLE {table} IS {_q(comment)}")
            else:
                op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_q(comment)}")


def downgrade():
    # Mirror upgrade()'s dependency-respecting order (each step inverted), NOT the
    # literal reverse: the index-create and comment restoration both reference the
    # column by name, and the CHECK re-add validates against existing rows at ADD
    # time on PostgreSQL. So: rename column → rebuild index → revert data → swap
    # constraint → restore comments.

    # 1. Rename the column back.
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.alter_column("project_entity_id", new_column_name="client_entity_id")

    # 2. Recreate the index under its old name (column is now client_entity_id).
    op.execute("DROP INDEX IF EXISTS ix_entity_managers_project_entity_id")
    op.create_index("ix_entity_managers_client_entity_id", "entity_managers", ["client_entity_id"])

    # 3. Drop the CHECK constraint before reverting the discriminator value:
    #    the UPDATE is validated against the current constraint, and PostgreSQL
    #    also validates CHECK against existing rows at ADD time.
    op.execute("ALTER TABLE entities DROP CONSTRAINT ck_entities_type")
    op.execute("UPDATE entities SET entity_type = 'client' WHERE entity_type = 'project'")

    # 4. Swap the CHECK constraint back to ('user', 'client').
    op.execute("ALTER TABLE entities ADD CONSTRAINT ck_entities_type CHECK (entity_type IN ('user', 'client'))")

    # 5. Restore the old schema comments (PostgreSQL only) — last, because the
    #    entity_managers comment references client_entity_id by name.
    if _is_postgresql():
        _old_comments = [
            ("entities", None, "Human users (OAuth) and programmatic clients (API key); entity_type distinguishes them"),
            ("entities", "entity_type", "'user' for OAuth users, 'client' for API clients"),
            ("entities", "email", "User email address; null for clients"),
            ("entities", "model_access_default", "Default model access policy: whitelist, blacklist, or graylist; client entities only"),
            ("entity_managers", None, "Maps users to client entities they are permitted to manage"),
            ("entity_managers", "user_entity_id", "The user who has management rights over the client"),
            ("entity_managers", "client_entity_id", "The client entity being managed"),
        ]
        for table, column, comment in _old_comments:
            if column is None:
                op.execute(f"COMMENT ON TABLE {table} IS {_q(comment)}")
            else:
                op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_q(comment)}")
