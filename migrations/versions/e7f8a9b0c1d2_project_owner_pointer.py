"""Replace entity_managers.is_owner with entities.owner_entity_id

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-05 00:00:00.000000

Project ownership moves from a flag on the membership row to a pointer on the
project row, so the database enforces *exactly* one owner per project:

* ``ck_entities_project_owner`` — every project has an owner, users never do.
* ``fk_entities_owner_membership`` — ``(owner_entity_id, id)`` references the
  owner's ``entity_managers`` row, so the owner is always a manager and that
  row cannot be deleted while they own the project. ``DEFERRABLE INITIALLY
  DEFERRED`` lets a project and its owner's row be inserted in one transaction.

The pointer is backfilled from ``is_owner = true``. A project with no owner
cannot be backfilled, so the upgrade aborts and lists those project IDs; an
admin must assign owners first. batch_alter_table keeps the statements
portable; SQLite dev uses ``create_all`` + stamp head and never runs this chain.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def _q(text):
    """Escape a comment string for use in a PostgreSQL dollar-quoted literal."""
    return f"$comment${text}$comment$"


def upgrade():
    ownerless = op.get_bind().execute(sa.text(
        """
        SELECT id FROM entities e
        WHERE e.entity_type = 'project' AND NOT EXISTS (
            SELECT 1 FROM entity_managers m
            WHERE m.project_entity_id = e.id AND m.is_owner
        )
        ORDER BY id
        """
    )).scalars().all()
    if ownerless:
        raise RuntimeError(
            "Cannot add entities.owner_entity_id: these projects have no owner: "
            + ", ".join(str(i) for i in ownerless)
            + ". Make one of each project's managers its owner, then re-run the migration."
        )

    with op.batch_alter_table("entities") as batch_op:
        batch_op.add_column(sa.Column("owner_entity_id", sa.Integer(), nullable=True))

    op.execute(sa.text(
        """
        UPDATE entities SET owner_entity_id = (
            SELECT m.user_entity_id FROM entity_managers m
            WHERE m.project_entity_id = entities.id AND m.is_owner
        )
        WHERE entity_type = 'project'
        """
    ))

    with op.batch_alter_table("entities") as batch_op:
        batch_op.create_check_constraint(
            "ck_entities_project_owner",
            "(entity_type = 'project' AND owner_entity_id IS NOT NULL)"
            " OR (entity_type <> 'project' AND owner_entity_id IS NULL)",
        )
        batch_op.create_foreign_key(
            "fk_entities_owner_membership",
            "entity_managers",
            ["owner_entity_id", "id"],
            ["user_entity_id", "project_entity_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )

    op.drop_index("uq_entity_managers_owner", table_name="entity_managers")
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.drop_column("is_owner")

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN entities.owner_entity_id IS "
            f"{_q('Owning user; required for projects, null for users')}"
        )


def downgrade():
    with op.batch_alter_table("entity_managers") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_owner",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    op.execute(sa.text(
        """
        UPDATE entity_managers SET is_owner = true
        WHERE EXISTS (
            SELECT 1 FROM entities e
            WHERE e.id = entity_managers.project_entity_id
              AND e.owner_entity_id = entity_managers.user_entity_id
        )
        """
    ))
    op.create_index(
        "uq_entity_managers_owner",
        "entity_managers",
        ["project_entity_id"],
        unique=True,
        postgresql_where=sa.text("is_owner"),
        sqlite_where=sa.text("is_owner"),
    )

    with op.batch_alter_table("entities") as batch_op:
        batch_op.drop_constraint("fk_entities_owner_membership", type_="foreignkey")
        batch_op.drop_constraint("ck_entities_project_owner", type_="check")
        batch_op.drop_column("owner_entity_id")

    if _is_postgresql():
        op.execute(
            f"COMMENT ON COLUMN entity_managers.is_owner IS "
            f"{_q('True for the project owner; at most one owner per project (enforced by app logic)')}"
        )
