"""Add auth_requests.requested_project, project_entity_id and denial_reason

Revision ID: e9a1c3d5f7b2
Revises: b7c9d1e3f5a7
Create Date: 2026-11-24 00:00:00.000000

OAuth key requests can ask for a project key. ``requested_project`` keeps the
raw ``project`` value the requester sent (a project UUID or name); it is
resolved only once a user is logged in. ``project_entity_id`` is the resolved
project, set at approval. ``denial_reason`` is the text the token endpoint
returns with ``access_denied``, fixed when the request is denied. All three are
nullable, so existing rows stay personal requests with no reason.
"""

import sqlalchemy as sa
from alembic import op

revision = "e9a1c3d5f7b2"
down_revision = "b7c9d1e3f5a7"
branch_labels = None
depends_on = None

_REQUESTED_COMMENT = "Raw project UUID or name the requester sent (unverified); null for a personal key request"
_PROJECT_COMMENT = "Project the key is minted for, resolved from requested_project at approval; null for a personal key"
_DENIAL_COMMENT = "Reason returned as error_description with access_denied; null for a plain deny"


def upgrade():
    with op.batch_alter_table("auth_requests") as batch_op:
        batch_op.add_column(sa.Column("requested_project", sa.String(256), nullable=True, comment=_REQUESTED_COMMENT))
        batch_op.add_column(sa.Column("project_entity_id", sa.Integer(), nullable=True, comment=_PROJECT_COMMENT))
        batch_op.add_column(sa.Column("denial_reason", sa.String(512), nullable=True, comment=_DENIAL_COMMENT))
        batch_op.create_foreign_key(
            "fk_auth_requests_project_entity_id", "entities",
            ["project_entity_id"], ["id"], ondelete="CASCADE",
        )


def downgrade():
    with op.batch_alter_table("auth_requests") as batch_op:
        batch_op.drop_constraint("fk_auth_requests_project_entity_id", type_="foreignkey")
        batch_op.drop_column("denial_reason")
        batch_op.drop_column("project_entity_id")
        batch_op.drop_column("requested_project")
